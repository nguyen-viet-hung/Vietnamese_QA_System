import gc
import math
import os
import random
import shutil
import logging
import time
import warnings
import datetime
from typing import List

os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["TORCHELASTIC_ERROR_FILE"] = "src/models/runs/logs/ERROR_file.txt"
import sys
sys.path.insert(0,r'./')
import psutil
import threading

import wandb
import numpy as np

import torch
try:
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
except Exception:
    raise "Please update your pytorch, this script require a version higher than 1.7 with cuda"
import deepspeed
import torch.nn as nn
from torch.cuda.amp import autocast
from torch.distributed.elastic.multiprocessing.errors import record

from accelerate import Accelerator, dispatch_model
from accelerate.logging import get_logger
from accelerate.utils import \
    (DistributedType,
     release_memory,
     get_balanced_memory,
     infer_auto_device_map,
     DummyScheduler,
     DummyOptim, is_xpu_available)
from accelerate.state import AcceleratorState

import datasets, transformers
from transformers import \
    (AutoModelForCausalLM,
     AutoModelForSeq2SeqLM,
     get_scheduler,
     set_seed,
     BitsAndBytesConfig,
     GenerationConfig,
     AutoConfig,
     pipeline)
from transformers.trainer_pt_utils import get_parameter_names
from transformers.utils import send_example_telemetry

import bitsandbytes as bnb
from peft import LoraConfig, TaskType, get_peft_model, PeftModel, prepare_model_for_kbit_training
from peft.utils.other import fsdp_auto_wrap_policy

from src.models.model_utils import poor_man_llm_load
from src.utils import in_notebook, timeit, b2mb

if in_notebook():
    try:
        from tqdm import tqdm_notebook as tqdm
    except ImportError as e:
        from tqdm.auto import tqdm
else:
    from tqdm.auto import tqdm

logger = get_logger(__name__)
PROJECT_NAME = "Vietnamese_Instruct_LLM"


def merge_adapter(base_model_name: str, peft_adapter: PeftModel,
                  adapter_save_path: str, adapter_name: str, main_process: bool,
                  model_type: str="CAUSAL_LM", model_dtype=None, shard_model: bool=False,
                  max_memory: dict={0: "0.3GB"}, max_shard_size: str="500MB",
                  no_split_module_classes: List[str]=None, accelerator=None):
    peft_adapter.save_pretrained(adapter_save_path,
                                 is_main_process=accelerator.is_main_process,
                                 save_function=accelerator.save,
                                 state_dict=accelerator.get_state_dict(peft_adapter, unwrap=False),
                                 )
    adapter_path_file = os.path.join(adapter_save_path, adapter_name)

    offload_config = {
        "offload_folder": "offload_inf",
        "torch_dtype": model_dtype,
        "use_cache": True,
        "offload_state_dict": True,
        "low_cpu_mem_usage": True,
        "trust_remote_code":True,
        "max_memory": max_memory
    }

    if model_type == "CAUSAL_LM":
        if not shard_model:
            base_model = AutoModelForCausalLM.from_pretrained(base_model_name,
                                                              **offload_config
                                                              )
        else:
            base_model = poor_man_llm_load(base_model_name, model_type=model_type,
                                           model_dtype=model_dtype, max_shard_size=max_shard_size,
                                           additional_kwargs=offload_config)
    elif model_type == "SEQ_2_SEQ_LM":
        if not shard_model:
            base_model = AutoModelForSeq2SeqLM.from_pretrained(base_model_name,
                                                               **offload_config
                                                               )
        else:
            base_model = poor_man_llm_load(base_model_name, model_type=model_type,
                                           model_dtype=model_dtype, max_shard_size=max_shard_size,
                                           additional_kwargs=offload_config)

    if getattr(base_model, "quantization_method", None) == "gptq":
        warnings.warn(f"The model {base_model_name} is gptq quantized and cannot be merged to LORA layers.\n"
                      f"Returning the original adapter...")
        del base_model
        gc.collect()
        return peft_adapter

    max_memory = get_balanced_memory(
        base_model,
        max_memory=None,
        no_split_module_classes=no_split_module_classes,
        dtype=model_dtype,
        low_zero=False,
    )

    device_map = infer_auto_device_map(
        base_model,
        max_memory=max_memory,
        no_split_module_classes=no_split_module_classes,
        dtype=model_dtype
    )

    base_model = dispatch_model(base_model, device_map=device_map, offload_dir="offload_inf")

    model_to_merge = PeftModel.from_pretrained(base_model,
                                               adapter_path_file,
                                               offload_folder="offload_inf",
                                               torch_dtype=model_dtype,
                                               max_memory=max_memory
                                               )

    merged_model = model_to_merge.merge_and_unload(progressbar=True)
    del base_model, peft_adapter, model_to_merge
    gc.collect()

    return merged_model


def prepare_any(prepare_dict: dict, distributed_type, accelerator):
    def get_grouped_parameters(adapter, weight_decay, is_deepspeed: bool=False):
        if is_deepspeed: return adapter.parameters()
        decay_parameters = get_parameter_names(adapter, [nn.LayerNorm])
        decay_parameters = [name for name in decay_parameters if "bias" not in name]
        optimizer_grouped_parameters = [
            {
                "params": [p for n, p in adapter.named_parameters() if n in decay_parameters],
                "weight_decay": weight_decay,
            },
            {
                "params": [p for n, p in adapter.named_parameters() if n not in decay_parameters],
                "weight_decay": 0.0,
            },
        ]

        return optimizer_grouped_parameters
    dataloaders = {}
    if distributed_type != DistributedType.DEEPSPEED:
        adapter = accelerator.prepare(prepare_dict['adapter'])
        optimizer_grouped_parameters = get_grouped_parameters(prepare_dict['adapter'], prepare_dict['weight_decay'])
        optimizer = getattr(bnb.optim, prepare_dict['optim_name'])(optimizer_grouped_parameters, lr=prepare_dict['lr'])
        accelerator.print(f"\nLoading {prepare_dict['optim_name']} from bits and bytes...")
        lr_scheduler = get_scheduler(
            name=prepare_dict['lr_sheduler_name'],
            optimizer=optimizer,
            num_warmup_steps=prepare_dict["warmup_steps"],
            num_training_steps=prepare_dict["max_train_steps"],
        )
        dataloaders['train_dataloader'], optimizer, lr_scheduler = accelerator.prepare(
            prepare_dict['train_dataloader'], optimizer, lr_scheduler
        )
    else:
        # Creates Dummy Optimizer if `optimizer` was specified in the config
        # file else creates Adam Optimizer
        optimizer_grouped_parameters = get_grouped_parameters(prepare_dict['adapter'],
                                                              prepare_dict['weight_decay'],
                                                              is_deepspeed=distributed_type == DistributedType.DEEPSPEED)
        optimizer_cls = (
            getattr(bnb.optim, prepare_dict['optim_name'])
            if accelerator.state.deepspeed_plugin is None
               or "optimizer" not in accelerator.state.deepspeed_plugin.deepspeed_config
            else DummyOptim
        )
        optimizer = optimizer_cls(optimizer_grouped_parameters, lr=prepare_dict['lr'])
        # Creates Dummy Scheduler if `scheduler` was specified in the config
        # file else creates `self.lr_scheduler_type` Scheduler
        if "scheduler" in accelerator.state.deepspeed_plugin.deepspeed_config:
            lr_scheduler = DummyScheduler(
                optimizer,
                total_num_steps=prepare_dict["max_train_steps"],
                warmup_num_steps=prepare_dict['warmup_steps']
            )
        else:
            lr_scheduler = get_scheduler(
                name=prepare_dict['lr_sheduler_name'],
                optimizer=optimizer,
                num_warmup_steps=prepare_dict["warmup_steps"],
                num_training_steps=prepare_dict["max_train_steps"],
            )
            adapter, dataloaders['train_dataloader'], optimizer, lr_scheduler = accelerator.prepare(
                prepare_dict['adapter'], prepare_dict['train_dataloader'], optimizer, lr_scheduler
            )

    if "perplexity_eval_dataloader" in prepare_dict:
        dataloaders['perplexity_eval_dataloader'] = accelerator.prepare(prepare_dict["perplexity_eval_dataloader"])
    if "generative_eval_dataloader" in prepare_dict:
        dataloaders['generative_eval_dataloader'] = accelerator.prepare(prepare_dict["generative_eval_dataloader"])
    if "test_dataloader" in prepare_dict:
        dataloaders['test_dataloader'] = accelerator.prepare(prepare_dict["test_dataloader"])

    return adapter, optimizer, dataloaders, lr_scheduler


# This context manager is used to track the peak memory usage of the process
class TorchTracemalloc:
    def __enter__(self):
        gc.collect()
        torch.cuda.empty_cache()
        torch.cuda.reset_max_memory_allocated()  # reset the peak gauge to zero
        self.begin = torch.cuda.memory_allocated()
        self.process = psutil.Process()

        self.cpu_begin = self.cpu_mem_used()
        self.peak_monitoring = True
        peak_monitor_thread = threading.Thread(target=self.peak_monitor_func)
        peak_monitor_thread.daemon = True
        peak_monitor_thread.start()
        return self

    def cpu_mem_used(self):
        """get resident set size memory for the current process"""
        return self.process.memory_info().rss

    def peak_monitor_func(self):
        self.cpu_peak = -1

        while True:
            self.cpu_peak = max(self.cpu_mem_used(), self.cpu_peak)

            # can't sleep or will not catch the peak right (this comment is here on purpose)
            # time.sleep(0.001) # 1msec

            if not self.peak_monitoring:
                break

    def __exit__(self, *exc):
        self.peak_monitoring = False

        gc.collect()
        torch.cuda.empty_cache()
        self.end = torch.cuda.memory_allocated()
        self.peak = torch.cuda.max_memory_allocated()
        self.used = b2mb(self.end - self.begin)
        self.peaked = b2mb(self.peak - self.begin)

        self.cpu_end = self.cpu_mem_used()
        self.cpu_used = b2mb(self.cpu_end - self.cpu_begin)
        self.cpu_peaked = b2mb(self.cpu_peak - self.cpu_begin)


@record
@timeit
def train(training_args, qa_dataloader, qa_dataloader_instance):
    start_time = time.time()
    send_example_telemetry(training_args.dataset_name, training_args)

    accelerator_log_kwargs = {}

    if training_args.with_tracking:
        accelerator_log_kwargs["log_with"] = training_args.report_to
        accelerator_log_kwargs["project_dir"] = training_args.output_dir

    accelerator = Accelerator(gradient_accumulation_steps=training_args.gradient_accumulation_steps,
                              **accelerator_log_kwargs)

    # Get gradient accumulation steps from deepspeed config if available, else assign int value to deepspeed config if
    # 'auto' was detected
    if accelerator.state.deepspeed_plugin is not None:
        try:
            training_args.gradient_accumulation_steps = int(accelerator.state.deepspeed_plugin.deepspeed_config[
                "gradient_accumulation_steps"
            ])
            accelerator.print(f" gradient_accumulation_steps was specified in the DS config, override the "
                              f"GA from CMD args\n")
        except ValueError:
            accelerator.print(f" Deepspeed config gradient_accumulation_steps is auto,"
                              f" switching to CMD args GA: {training_args.gradient_accumulation_steps}\n")

    accelerator.print(f"{AcceleratorState()}")

    # Make one log on every process with the configuration for debugging.
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
    )
    logger.info(accelerator.state, main_process_only=False)
    if accelerator.is_local_main_process:
        datasets.utils.logging.set_verbosity_warning()
        transformers.utils.logging.set_verbosity_info()
    else:
        datasets.utils.logging.set_verbosity_error()
        transformers.utils.logging.set_verbosity_error()

    lora_r = training_args.lora_r
    lora_alpha = training_args.lora_alpha
    lora_dropout = training_args.lora_dropout

    use_4bit = training_args.use_4bit
    bnb_4bit_compute_dtype = training_args.bnb_4bit_compute_dtype
    bnb_4bit_quant_type = training_args.bnb_4bit_quant_type
    use_nested_quant = training_args.use_nested_quant

    use_8bit = training_args.use_8bit

    model_name_or_path = training_args.model_name_or_path
    gradient_accumulation_steps = training_args.gradient_accumulation_steps
    lr_sheduler_name = training_args.lr_sheduler_name
    dataset_name = training_args.dataset_name
    text_column = training_args.text_column
    label_column = training_args.label_column
    lr = training_args.lr
    num_epochs = training_args.num_epochs
    seed = training_args.seed
    do_test = training_args.do_test
    do_eval = training_args.do_eval
    gradient_checkpointing = training_args.gradient_checkpointing
    weight_decay = training_args.weight_decay
    target_modules = training_args.target_modules
    task_type = training_args.model_type
    context_length = training_args.context_length
    model_offload = training_args.enable_model_offload
    llm_int8_cpu_offload = training_args.llm_int8_enable_fp32_cpu_offload
    optim_name = training_args.optim_name
    model_dtype = training_args.model_dtype
    perplexity_eval = training_args.do_perplexity_eval
    generative_eval = training_args.do_generative_eval
    merge_weight_eval = training_args.merge_weight_eval
    print_model_key = training_args.print_model_key

    top_k = training_args.top_k
    do_sample = training_args.do_sample
    no_repeat_ngram_size = training_args.no_repeat_ngram_size
    num_beams = training_args.num_beams
    early_stopping = training_args.early_stopping
    max_time = training_args.max_time
    penalty_alpha = training_args.penalty_alpha
    repetition_penalty = training_args.repetition_penalty
    temperature = training_args.temperature
    no_truncation = training_args.no_truncation
    encoder_repetition_penalty = training_args.encoder_repetition_penalty
    max_length = training_args.max_length
    max_new_tokens = training_args.max_new_tokens
    shard_model = training_args.shard_model
    max_model_shard_size = training_args.max_model_shard_size
    deep_speed_inf = training_args.deep_speed_inf
    top_p = training_args.top_p
    injection_policy = training_args.injection_policy
    auto_kernel_injection = training_args.auto_kernel_injection
    use_default_gen_config = training_args.use_default_gen_config
    shard_model_merge = training_args.shard_model_merge
    minimum_free_spaces = training_args.minimum_free_spaces
    use_flash_attention_2 = training_args.use_flash_attention_2
    lora_bias = training_args.lora_bias
    modules_to_save = training_args.modules_to_save
    warmup_steps = training_args.warmup_steps
    no_split_module_classes = training_args.no_split_module_classes
    resume_from_checkpoint = training_args.resume_from_checkpoint
    report_to = training_args.report_to
    with_tracking = training_args.with_tracking
    convert_cpkt = training_args.convert_cpkt
    checkpointing_steps = training_args.checkpointing_steps
    checkpoint_at_max_time = training_args.checkpoint_at_max_time

    set_seed(seed)

    if not use_default_gen_config:
        try:
            generation_config, unused_config = GenerationConfig.from_pretrained(
                model_name_or_path, top_k=top_k, do_sample=do_sample, return_unused_kwargs=True,
                no_repeat_ngram_size=no_repeat_ngram_size, num_beams=num_beams, early_stopping=early_stopping,
                max_time=max_time, penalty_alpha=penalty_alpha, repetition_penalty=repetition_penalty, temperature=temperature,
                truncation=not no_truncation, encoder_repetition_penalty=encoder_repetition_penalty, max_length=max_length,
                max_new_tokens=max_new_tokens, top_p=top_p, use_cache=True, low_memory=True
            )
            if len(unused_config) > 0: accelerator.print(f"Unused config: {unused_config}")
        except Exception as e:
            warnings.warn(f"The model {model_name_or_path} does not have a generation config")
            warnings.warn(f"Error message: {e}")
            generation_config = GenerationConfig.from_dict(config_dict={
                "top_k": top_k, "do_sample": do_sample, "no_repeat_ngram_size": no_repeat_ngram_size, "num_beams": num_beams, "early_stopping": early_stopping,
                "max_time": max_time, "penalty_alpha": penalty_alpha, "repetition_penalty": repetition_penalty,  "max_new_tokens": max_new_tokens,
                "temperature": temperature, "encoder_repetition_penalty": encoder_repetition_penalty,
                "max_length": max_length, "truncation": not no_truncation, "top_p": top_p, "use_cache": True, "low_memory": True
            })
    else:
        generation_config = GenerationConfig.from_dict(config_dict={"min_new_tokens": 20,
                                                                    "max_length": context_length,
                                                                    "max_time": max_time})
    accelerator.print(f"Model generation config: {generation_config}")

    tokenizer = qa_dataloader.tokenizer

    accelerator.print(" Print out a couple samples for tokenizer compatibility check for multilingual task")
    for idx, data in enumerate(iter(qa_dataloader_instance['test']['perplexity_eval'])):
        accelerator.print("\n==============================================================================\n")
        accelerator.print("\n Input: "+qa_dataloader.tokenizer.decode(data['input_ids'][0], skip_special_tokens=False))
        labels = data['labels'].cpu().numpy()
        labels = np.where(labels != -100, labels, qa_dataloader.tokenizer.pad_token_id)
        accelerator.print("\n Label:"+qa_dataloader.tokenizer.decode(labels[0], skip_special_tokens=False))
        accelerator.print("\n==============================================================================\n")
        if idx == 10: break

    try:
        config = AutoConfig.from_pretrained(
            model_name_or_path,
        )
    except Exception:
        warnings.warn(f"Model {model_name_or_path} does not have a config.json")
        config = None

    # System setup info
    free_in_GB = int(torch.cuda.mem_get_info()[0] / 1024 ** 3)
    max_memory = f'{int(torch.cuda.mem_get_info()[0] / 1024 ** 3) - minimum_free_spaces}GB'
    n_gpus = torch.cuda.device_count()
    max_memory = {i: max_memory for i in range(n_gpus)}

    accelerator.print(f"System max memory: {max_memory}\n"
                      f"System num gpus: {n_gpus}\n"
                      f"System free in GB: {free_in_GB}")

    compute_dtype = getattr(torch, bnb_4bit_compute_dtype)
    if model_dtype != "auto":
        model_dtype = getattr(torch, model_dtype)
    task_type = getattr(TaskType, task_type)

    # Check GPU compatibility with bfloat16
    if compute_dtype == torch.float16 and use_4bit:
        major, _ = torch.cuda.get_device_capability()
        if major >= 8:
            accelerator.print("=" * 80)
            accelerator.print("Your GPU supports bfloat16: accelerate training with bf16=True")
            accelerator.print("=" * 80)

    peft_config = LoraConfig(
        task_type=task_type,
        inference_mode=False,
        r=lora_r, lora_alpha=lora_alpha,
        lora_dropout=lora_dropout,
        target_modules=target_modules,
        bias=lora_bias,
        modules_to_save=modules_to_save
    )

    if accelerator.distributed_type != DistributedType.DEEPSPEED:
        # Copy the model to each device
        # Naive pipeline parallelism
        device_map = (
            {"": f"xpu:{accelerator.local_process_index}"}
            if is_xpu_available()
            else {"": accelerator.local_process_index}
        ) if accelerator.distributed_type != DistributedType.NO else "auto"

        with accelerator.main_process_first():
            print(f"\nModel device map: {device_map} for process {accelerator.local_process_index}\n")

    else:
        device_map = None
        accelerator.print("\n Deeepspeed enabled, device_map will be handle by deepspeed\n")

    if use_4bit:
        quant_config = BitsAndBytesConfig(
            load_in_4bit=use_4bit,
            bnb_4bit_use_double_quant=use_nested_quant,
            bnb_4bit_compute_type=compute_dtype,
            bnb_4bit_quant_type=bnb_4bit_quant_type,
            llm_int8_enable_fp32_cpu_offload=llm_int8_cpu_offload,
            llm_int8_threshold=6.0,
        )
    elif use_8bit:
        quant_config = BitsAndBytesConfig(
            load_in_8bit=use_8bit,
            llm_int8_enable_fp32_cpu_offload=llm_int8_cpu_offload,
            llm_int8_threshold=6.0,
        )
    else:
        quant_config = None
        warnings.warn("\n   No quantization is applied")

    offload_config = {
        "device_map": device_map,
        "offload_folder": "offload",
        "offload_state_dict": True,
        "low_cpu_mem_usage": True,
        "max_memory": max_memory
    } if model_offload and accelerator.distributed_type != DistributedType.DEEPSPEED else {}

    full_model_config = {
        "quantization_config": quant_config,
        "trust_remote_code": True,
        "load_in_8bit": use_8bit,
        "load_in_4bit": use_4bit,
        "torch_dtype": model_dtype,
        "config": config,
    }

    if use_flash_attention_2: full_model_config["use_flash_attention_2"] = True

    if "gpt2" in model_name_or_path:
        full_model_config["scale_attn_by_inverse_layer_idx"] = True
        full_model_config["reorder_and_upcast_attn"] = True

    if model_offload: full_model_config = {**full_model_config, **offload_config}

    # creating model
    if task_type == "CAUSAL_LM":
        if not shard_model:
            base_model = AutoModelForCausalLM.from_pretrained(model_name_or_path,
                                                              **full_model_config)
        else:
            base_model = poor_man_llm_load(model_name_or_path, model_type=task_type,
                                           model_dtype=model_dtype, max_shard_size=max_model_shard_size,
                                           additional_kwargs=full_model_config)
    elif task_type == "SEQ_2_SEQ_LM":
        if not shard_model:
            base_model = AutoModelForSeq2SeqLM.from_pretrained(model_name_or_path,
                                                              **full_model_config)
        else:
            base_model = poor_man_llm_load(model_name_or_path, model_type=task_type,
                                           model_dtype=model_dtype, max_shard_size=max_model_shard_size,
                                           additional_kwargs=full_model_config)

    accelerator.print(f"\n  Base model memory footprint: {base_model.get_memory_footprint()}\n")

    if accelerator.distributed_type == DistributedType.NO:
        max_memory = get_balanced_memory(
            base_model,
            max_memory=None,
            no_split_module_classes=no_split_module_classes,
            dtype=model_dtype,
            low_zero=False,
        )

        accelerator.print(f"\nMax balance memory: {max_memory}\n")

        device_map = infer_auto_device_map(
            base_model,
            max_memory=max_memory,
            no_split_module_classes=no_split_module_classes,
            dtype=model_dtype
        )

        accelerator.print(f"\nModel device map to dispatch: {device_map}\n")

        base_model = dispatch_model(base_model,
                                    device_map=device_map,
                                    offload_dir="offload",
                                    )

    # We resize the embeddings only when necessary to avoid index errors. If you are creating a model from scratch
    # on a small vocab and want a smaller embedding size, remove this test.
    embedding_size = base_model.get_input_embeddings().weight.shape[0]
    accelerator.print(f"Model embedding size: {embedding_size}")
    accelerator.print(f"Tokenizer vocab size: {len(tokenizer)}")
    if len(tokenizer) > embedding_size:
        base_model.resize_token_embeddings(len(tokenizer))

    # Please enable gradient_checkpointing at all cost, this will save your life
    if use_4bit or use_8bit or getattr(base_model, "quantization_method", None) == "gptq":
        accelerator.print(f"Preparation for kbit training...")
        base_model = prepare_model_for_kbit_training(base_model,
                                                     use_gradient_checkpointing=gradient_checkpointing) # Prepare model in peft already include gradient-checkpoint, freeze params
    elif gradient_checkpointing:
        base_model.gradient_checkpointing_enable()
    else:
        warnings.warn("You disable gradient checkpoint, this will result in vram consumtion")

    base_model.config.use_cache = False

    if print_model_key:
        accelerator.print(base_model)

    # TODO: For cast weights to fp32
    if modules_to_save and use_8bit or use_4bit:
        pass

    adapter = get_peft_model(base_model, peft_config=peft_config, adapter_name=dataset_name)
    if gradient_checkpointing: adapter.gradient_checkpointing_enable() # Double check!
    adapter.print_trainable_parameters()

    if print_model_key:
        accelerator.print(adapter)

    num_update_steps_per_epoch = math.ceil(len(qa_dataloader_instance['train']) / gradient_accumulation_steps)
    max_train_steps = num_epochs * num_update_steps_per_epoch

    if getattr(accelerator.state, "fsdp_plugin", None) is not None:
        accelerator.print(f"FSDP detected, using FSDP...")
        accelerator.state.fsdp_plugin.auto_wrap_policy = fsdp_auto_wrap_policy(adapter)
        if print_model_key:
            accelerator.print(adapter)

    prepare_dict = {"adapter": adapter,
                    "train_dataloader": qa_dataloader_instance['train'],
                    "optim_name": optim_name,
                    "weight_decay": weight_decay,
                    "lr": lr,
                    "lr_sheduler_name": lr_sheduler_name,
                    "warmup_steps": warmup_steps,
                    "max_train_steps": max_train_steps}

    if do_eval:
        if perplexity_eval:
            prepare_dict["perplexity_eval_dataloader"] = qa_dataloader_instance['eval']['perplexity_eval']
        if generative_eval:
            prepare_dict["generative_eval_dataloader"] = qa_dataloader_instance['eval']['generative_eval']
    else:
        logger.info("\nEvaluation turn off for this session")
    if do_test:
        prepare_dict["test_dataloader"] = qa_dataloader_instance['test']
    else:
        logger.info("\nTest turn off for this session")

    accelerator.print("\n Dict items to prepare: \n")
    for key, value in prepare_dict.items():
        accelerator.print(f" {key}: {value}\n")

    adapter, optimizer, dataloaders, lr_scheduler = prepare_any(prepare_dict,
                                                                accelerator.distributed_type,
                                                                accelerator)
    train_dataloader = dataloaders['train_dataloader']
    if do_eval:
        if "perplexity_eval_dataloader" in dataloaders:
            perplexity_eval_dataloader = dataloaders["perplexity_eval_dataloader"]
        if "generative_eval_dataloader" in dataloaders:
            generative_eval_dataloader = dataloaders["generative_eval_dataloader"]
    if do_test:
        if "test_dataloader" in dataloaders:
            test_dataloader = dataloaders['test_dataloader']

    # We need to recalculate our total training steps as the size of the training dataloader may have changed.
    num_update_steps_per_epoch = math.ceil(len(qa_dataloader_instance['train']) / gradient_accumulation_steps)
    max_train_steps = num_epochs * num_update_steps_per_epoch
    # Afterwards we recalculate our number of training epochs
    num_epochs = math.ceil(max_train_steps / num_update_steps_per_epoch)

    # On TPU, the tie weights in our model have been disconnected, so we need to restore the ties.
    if accelerator.distributed_type == DistributedType.TPU:
        adapter.tie_weights()

    if checkpointing_steps or checkpoint_at_max_time or resume_from_checkpoint:
        accelerator.register_for_checkpointing(lr_scheduler)
        # Parse out whether we are saving every epoch or after a certain number of batches
        if hasattr(checkpointing_steps, "isdigit"):
            if checkpointing_steps == "epoch":
                checkpointing_steps = checkpointing_steps
            elif checkpointing_steps.isdigit():
                checkpointing_steps = int(checkpointing_steps)
            else:
                raise ValueError(
                    f"Argument `checkpointing_steps` must be either a number or `epoch`. `{checkpointing_steps}` passed."
                )
        else:
            checkpointing_steps = None

    # We need to keep track of how many total steps we have iterated over
    completed_steps = 0
    overall_step = 0
    # We also need to keep track of the stating epoch so files are named properly
    starting_epoch = 0
    resume_step = None

    if resume_from_checkpoint:
        if resume_from_checkpoint is not None or resume_from_checkpoint != "":
            accelerator.print(f"Resumed from checkpoint: {resume_from_checkpoint}")
            accelerator.load_state(resume_from_checkpoint)
            path = os.path.basename(resume_from_checkpoint)
        else:
            # Get the most recent checkpoint
            dirs = [f.name for f in os.scandir(os.getcwd()) if f.is_dir()]
            dirs.sort(key=os.path.getctime)
            path = dirs[-1]  # Sorts folders by date modified, most recent checkpoint is the last

        if not training_args.override_last_cpkt_step:
            # Extract `epoch_{i}` or `step_{i}`
            training_difference = os.path.splitext(path)[0]

            if "epoch" in training_difference:
                starting_epoch = int(training_difference.replace("epoch_", "")) + 1
                resume_step = None
            else:
                resume_step = int(training_difference.replace("step_", ""))
                starting_epoch = resume_step // len(train_dataloader)
                resume_step -= starting_epoch * len(train_dataloader)
        else:
            resume_step = None

    # We need to initialize the trackers we use, and also store our configuration.
    # The trackers initializes automatically on the main process.
    if with_tracking:
        experiment_config = vars(training_args)
        # TensorBoard cannot log Enums, need the raw value
        experiment_config["lr_scheduler_type"] = experiment_config["lr_sheduler_name"].value
        accelerator.init_trackers(PROJECT_NAME,
                                  config=experiment_config,
                                  init_kwargs={"wandb": {"name": f"{dataset_name}_completed_step{resume_step if resume_step is not None else 0}"}}
                                  )

    def save_push():
        accelerator.wait_for_everyone()
        unwrapped_adapter = accelerator.unwrap_model(adapter)
        unwrapped_adapter.save_pretrained(dataset_name,
                                          is_main_process=accelerator.is_main_process,
                                          save_function=accelerator.save,
                                          state_dict=accelerator.get_state_dict(unwrapped_adapter, unwrap=False),
                                          )
        if accelerator.is_main_process:
            unwrapped_adapter.push_to_hub(
                "1TuanPham/"
                + f"{dataset_name}_{model_name_or_path}_{peft_config.peft_type}_{peft_config.task_type}".replace(
                    "/",
                    "_"),
                state_dict=accelerator.get_state_dict(unwrapped_adapter, unwrap=False),
                use_auth_token=True,
                private=True
            )
        accelerator.wait_for_everyone()
        if with_tracking:
            accelerator.end_training()

    def save_state():
        nonlocal checkpoint_at_max_time
        output_cpkt_dir = f"step_{overall_step}"
        output_dir = os.path.join("src/models/runs/checkpoints", output_cpkt_dir)
        accelerator.save_state(output_dir)
        checkpoint_at_max_time += training_args.checkpoint_at_max_time
        if accelerator.is_main_process and training_args.log_weights_cpkt:
            if training_args.with_tracking and training_args.log_weights_cpkt:
                if training_args.report_to == "wandb":
                    wandb_artifact = wandb.Artifact(
                        name=f"{training_args.dataset_name}_{output_cpkt_dir}",
                        type='model',
                        description="Model checkpoint for VQA")
            accelerator.print(f"\nLogging checkpoint {output_dir} to wandb...\n")
            wandb_artifact.add_dir(output_dir)
            accelerator.get_tracker(name="wandb", unwrap=True).log_artifact(wandb_artifact)

    if resume_from_checkpoint and convert_cpkt:
        save_push()
        return True

    progress_bar_epoch = tqdm(total=num_epochs-starting_epoch, desc=f"Training progress on process {accelerator.process_index}",
                              position=accelerator.process_index,
                              colour="green")

    for epoch in range(starting_epoch, num_epochs):
        with TorchTracemalloc() as tracemalloc:
            adapter.train()
            total_loss = 0
            if resume_from_checkpoint and epoch == starting_epoch and resume_step is not None:
                # We need to skip steps until we reach the resumed step
                active_dataloader = accelerator.skip_first_batches(train_dataloader, resume_step)
                overall_step += resume_step
            else:
                # After the first iteration though, we need to go back to the original dataloader
                active_dataloader = train_dataloader
            progress_bar_step = tqdm(total=len(active_dataloader),
                                     desc=f"Training progress epoch {epoch} on process {accelerator.process_index}",
                                     position=accelerator.process_index,
                                     colour="blue")
            for step, batch in enumerate(active_dataloader):
                with accelerator.accumulate(adapter):
                    outputs = adapter(**batch)
                    loss = outputs.loss
                    total_loss += loss.detach().float()
                    accelerator.backward(loss)
                    optimizer.step()
                    lr_scheduler.step()
                    optimizer.zero_grad()

                overall_step += 1
                progress_bar_step.update(1)
                elapsed_time = (time.time() - start_time) / 3600

                # Checks if the accelerator has performed an optimization step behind the scenes
                if accelerator.sync_gradients:
                    rate = progress_bar_step.format_dict["rate"]
                    remaining = (progress_bar_step.total - progress_bar_step.n) / rate if rate and progress_bar_step.total else 0
                    current_loss = (total_loss / step).item()
                    if with_tracking:
                        accelerator.log(
                            {
                                "current_loss_batch": current_loss,
                                "overall_steps": overall_step,
                                "Elapsed(hours)": progress_bar_step.format_dict['elapsed'] / 60 / 60,
                                "Time_left(hours)": round(remaining / 60 / 60, 3),
                                "learning_rate": lr_scheduler.get_last_lr()[0]
                            },
                            step=completed_steps
                        )
                    completed_steps += 1
                    tqdm.write(f"\n Current loss: {current_loss}, step: {completed_steps}")
                    progress_bar_step.desc = f"Training progress E:{epoch}|P:{accelerator.process_index}|L:{round(current_loss, 4)}|S:{completed_steps}|T:{round(remaining/60/60, 3)}h|Elapsed:{round(elapsed_time, 2)}"
                    del loss, outputs, batch

                if isinstance(checkpointing_steps, int):
                    if overall_step % checkpointing_steps == 0:
                        save_state()

                if isinstance(checkpoint_at_max_time, float):
                    if elapsed_time >= checkpoint_at_max_time:
                        save_state()
                        checkpoint_at_max_time += training_args.checkpoint_at_max_time

        progress_bar_epoch.update(1)

        # Printing the GPU memory usage details such as allocated memory, peak memory, and total memory usage
        accelerator.print("GPU Memory before entering the train : {}".format(b2mb(tracemalloc.begin)))
        accelerator.print("GPU Memory consumed at the end of the train (end-begin): {}".format(tracemalloc.used))
        accelerator.print("GPU Peak Memory consumed during the train (max-begin): {}".format(tracemalloc.peaked))
        accelerator.print(
            "GPU Total Peak Memory consumed during the train (max): {}".format(
                tracemalloc.peaked + b2mb(tracemalloc.begin)
            )
        )

        accelerator.print("CPU Memory before entering the train : {}".format(b2mb(tracemalloc.cpu_begin)))
        accelerator.print("CPU Memory consumed at the end of the train (end-begin): {}".format(tracemalloc.cpu_used))
        accelerator.print("CPU Peak Memory consumed during the train (max-begin): {}".format(tracemalloc.cpu_peaked))
        accelerator.print(
            "CPU Total Peak Memory consumed during the train (max): {}".format(
                tracemalloc.cpu_peaked + b2mb(tracemalloc.cpu_begin)
            )
        )
        train_epoch_loss = total_loss / len(train_dataloader)
        train_ppl = torch.exp(train_epoch_loss)
        accelerator.print(f"{epoch=}: {train_ppl=} {train_epoch_loss=}")

        if checkpointing_steps == "epoch":
            output_dir = f"epoch_{epoch}"
            output_dir = os.path.join("src/models/runs/checkpoints", output_dir)
            accelerator.save_state(output_dir)

        if num_epochs - starting_epoch == 1:
            save_push()

        # TODO: Refactor evaluation
        if do_eval:
            cur_time = '_'.join(str(datetime.datetime.now().strftime('%Y-%m-%d %H-%M-%S')).split())
            if merge_weight_eval:
                if not getattr(base_model, "quantization_method", None) == "gptq":
                    accelerator.print(f"Merging model for faster inference...")
                    accelerator.wait_for_everyone()
                    inference_model = merge_adapter(model_name_or_path,
                                                    peft_adapter=accelerator.unwrap_model(adapter),
                                                    adapter_save_path=f"src/models/adapters/{dataset_name}-e{epoch}-{cur_time}",
                                                    main_process=accelerator.is_main_process, adapter_name=dataset_name,
                                                    model_type=task_type,
                                                    model_dtype=model_dtype,
                                                    shard_model=shard_model_merge,
                                                    max_memory=max_memory,
                                                    max_shard_size=max_model_shard_size,
                                                    no_split_module_classes=no_split_module_classes,
                                                    accelerator=accelerator)
                else:
                    warnings.warn(
                        f"The model {model_name_or_path} is gptq quantized and cannot be merged to LORA layers.\n"
                        f"Skipping merge_weight_eval...")
            else:
                warnings.warn(f"Weight from peft not merged yet, this may result in slower inference")
                accelerator.wait_for_everyone()
                inference_model = accelerator.unwrap_model(adapter)

            if deep_speed_inf:
                world_size = int(os.getenv('WORLD_SIZE', str(torch.cuda.device_count())))
                os.environ["RANK"] = "0"
                os.environ["LOCAL_RANK"] = "0"
                os.environ["WORLD_SIZE"] = str(torch.cuda.device_count())

                # The injection_policy shows two things:
                #   1. which layer module we need to add Tensor-Parallelism
                #   2. the name of several linear layers: a) attention_output (both encoder and decoder),
                #       and b) transformer output
                accelerator.print(f"Model type for inference: {type(inference_model)}")
                injection_config = {
                    "replace_with_kernel_inject": auto_kernel_injection,
                    "injection_policy": injection_policy
                } if auto_kernel_injection or injection_policy else {}

                inference_model = deepspeed.init_inference(
                    inference_model,
                    mp_size=world_size,
                    **injection_config
                )

            inference_model.eval()
            if task_type == "SEQ_2_SEQ_LM" and generative_eval:
                eval_preds = []
                if generative_eval:
                    with TorchTracemalloc() as tracemalloc:
                        with torch.no_grad():
                            for idx, batch in enumerate(
                                    tqdm(generative_eval_dataloader,
                                         desc=f"Evaluating epoch {epoch} generative on process {accelerator.process_index}",
                                         position=accelerator.process_index,
                                         colour="blue")):
                                # Pass dummy batch to avoid caffe error
                                if idx == 0 and accelerator.distributed_type != DistributedType.NO:
                                    inference_model(**batch)
                                batch = {k: v for k, v in batch.items() if k != "labels"}
                                outputs = inference_model.generate(
                                    **batch, generation_config=generation_config,
                                    synced_gpus=True if accelerator.distributed_type != DistributedType.NO else False,
                                    pad_token_id=tokenizer.pad_token_id
                                )  # synced_gpus=True for Distributed training
                                outputs = accelerator.pad_across_processes(outputs, dim=1, pad_index=tokenizer.pad_token_id)
                                preds = accelerator.gather_for_metrics(outputs).detach().cpu().numpy()
                                eval_preds.extend(tokenizer.batch_decode(preds, skip_special_tokens=True))

                    # Printing the GPU memory usage details such as allocated memory, peak memory, and total memory usage
                    accelerator.print("GPU Memory before entering the eval : {}".format(b2mb(tracemalloc.begin)))
                    accelerator.print(
                        "GPU Memory consumed at the end of the eval (end-begin): {}".format(tracemalloc.used))
                    accelerator.print(
                        "GPU Peak Memory consumed during the eval (max-begin): {}".format(tracemalloc.peaked))
                    accelerator.print(
                        "GPU Total Peak Memory consumed during the eval (max): {}".format(
                            tracemalloc.peaked + b2mb(tracemalloc.begin)
                        )
                    )

                    accelerator.print("CPU Memory before entering the eval : {}".format(b2mb(tracemalloc.cpu_begin)))
                    accelerator.print(
                        "CPU Memory consumed at the end of the eval (end-begin): {}".format(tracemalloc.cpu_used))
                    accelerator.print(
                        "CPU Peak Memory consumed during the eval (max-begin): {}".format(tracemalloc.cpu_peaked))
                    accelerator.print(
                        "CPU Total Peak Memory consumed during the eval (max): {}".format(
                            tracemalloc.cpu_peaked + b2mb(tracemalloc.cpu_begin)
                        )
                    )

                try:
                    cur_dir = os.getcwd()
                    if '/' in model_name_or_path:
                        model_name = model_name_or_path.replace("/", "-")
                    else:
                        model_name = model_name_or_path
                    log_path = os.path.join(cur_dir, f"src/models/runs/logs/log_dir_e{epoch}_{model_name}_{cur_time}.txt")
                    with open(log_path, 'w') as log_file:
                        # Log info
                        log_file.write(f"\n       {epoch=}: {train_ppl=} {train_epoch_loss=}\n")
                        # log_file.write(f"\n       Accuracy: {accuracy}\n")
                        for i in range(0, 10):
                            idx = random.randint(0, len(eval_preds)-1)
                            accelerator.print(f"        Question: {qa_dataloader.dataset['eval'][idx][text_column]}\n")
                            accelerator.print(f"    Evaluation prediction: {eval_preds[idx]}\n")
                            accelerator.print(f"    Actual label: {qa_dataloader.dataset['eval'][idx][label_column]}\n")

                            log_file.write("===================================================================\n")
                            log_file.write(f"Question: {qa_dataloader.dataset['eval'][idx][text_column]}\n")
                            log_file.write(f"Evaluation prediction: {eval_preds[idx]}\n")
                            log_file.write(f"Actual label: {qa_dataloader.dataset['eval'][idx][label_column]}\n")
                            log_file.write("===================================================================\n")
                        log_file.write(f"\n     Training arguments: \n")
                        for key, value in vars(training_args).items():
                            log_file.write(f"\n {key}: {value} ")

                except Exception as e:
                    warnings.warn(f"Can't save config for this run {epoch}\n"
                                  f"Error message: {e}")
                    pass

            elif task_type == "CAUSAL_LM":
                eval_preds = []
                if generative_eval:
                    with TorchTracemalloc() as tracemalloc:
                        with torch.no_grad():
                            with autocast():
                                with torch.backends.cuda.sdp_kernel(enable_flash=True, enable_math=False,
                                                                    enable_mem_efficient=False):
                                    for idx, batch in enumerate(
                                            tqdm(generative_eval_dataloader,
                                                 desc=f"Evaluating epoch {epoch} generative on process {accelerator.process_index}",
                                                 position=accelerator.process_index,
                                                 colour="blue")):
                                        # Pass dummy batch to avoid caffe error
                                        if idx == 0 and accelerator.distributed_type != DistributedType.NO:
                                            inference_model(**batch)
                                        batch = {k: v for k, v in batch.items() if k != "labels"}
                                        with torch.no_grad():
                                            outputs = inference_model.generate(
                                                **batch, generation_config=generation_config,
                                                synced_gpus=accelerator.distributed_type != DistributedType.NO,
                                                pad_token_id=tokenizer.pad_token_id
                                            )  # synced_gpus=True for Distributed training
                                        outputs = accelerator.pad_across_processes(outputs, dim=1, pad_index=tokenizer.pad_token_id)
                                        preds = accelerator.gather_for_metrics(outputs).detach().cpu().numpy()
                                        eval_preds.extend(tokenizer.batch_decode(preds, skip_special_tokens=True))

                    # Printing the GPU memory usage details such as allocated memory, peak memory, and total memory usage
                    accelerator.print("GPU Memory before entering the eval : {}".format(b2mb(tracemalloc.begin)))
                    accelerator.print(
                        "GPU Memory consumed at the end of the eval (end-begin): {}".format(tracemalloc.used))
                    accelerator.print(
                        "GPU Peak Memory consumed during the eval (max-begin): {}".format(tracemalloc.peaked))
                    accelerator.print(
                        "GPU Total Peak Memory consumed during the eval (max): {}".format(
                            tracemalloc.peaked + b2mb(tracemalloc.begin)
                        )
                    )

                    accelerator.print("CPU Memory before entering the eval : {}".format(b2mb(tracemalloc.cpu_begin)))
                    accelerator.print(
                        "CPU Memory consumed at the end of the eval (end-begin): {}".format(tracemalloc.cpu_used))
                    accelerator.print(
                        "CPU Peak Memory consumed during the eval (max-begin): {}".format(tracemalloc.cpu_peaked))
                    accelerator.print(
                        "CPU Total Peak Memory consumed during the eval (max): {}".format(
                            tracemalloc.cpu_peaked + b2mb(tracemalloc.cpu_begin)
                        )
                    )

                perplexity = 0
                if perplexity_eval:
                    inference_model.eval()
                    losses = []
                    for step, batch in enumerate(tqdm(perplexity_eval_dataloader,
                                                      desc=f"Evaluating epoch {epoch} perplexity",
                                                      position=accelerator.process_index,
                                                      colour="blue")):
                        with torch.no_grad():
                            outputs = inference_model(**batch)

                        loss = outputs.loss
                        losses.append(accelerator.gather_for_metrics(loss.repeat(qa_dataloader.perplexity_eval_batch_size)))

                    losses = torch.cat(losses)
                    try:
                        eval_loss = torch.mean(losses)
                        perplexity = math.exp(eval_loss)
                    except OverflowError:
                        perplexity = float("inf")

                    accelerator.print(f"epoch {epoch}: perplexity: {perplexity} eval_loss: {eval_loss}")

                if generative_eval:
                    try:
                        cur_dir = os.getcwd()
                        if '/' in model_name_or_path:
                            model_name = model_name_or_path.replace("/", "-")
                        else:
                            model_name = model_name_or_path
                        log_path = os.path.join(cur_dir, f"src/models/runs/logs/log_dir_e{epoch}_{model_name}_{cur_time}.txt")
                        with open(log_path, 'w') as log_file:
                            # Log info
                            log_file.write(f"\n       {epoch=}: {train_ppl=} {train_epoch_loss=}\n")
                            log_file.write(f"\n       Perplexity: {perplexity}\n")
                            for idx in range(0, len(eval_preds)-1):
                                try:
                                    accelerator.print(f"        Question:\n {qa_dataloader.dataset['eval'][idx][text_column]}\n")
                                    accelerator.print(f"    Evaluation prediction:\n {eval_preds[idx]}\n")
                                    accelerator.print(f"    Actual label:\n {qa_dataloader.dataset['eval'][idx][label_column]}\n")

                                    log_file.write("===================================================================\n")
                                    log_file.write(f"Question:\n {qa_dataloader.dataset['eval'][idx][text_column]}\n")
                                    log_file.write(f"Evaluation prediction:\n {eval_preds[idx]}\n")
                                    log_file.write(f"Actual label:\n {qa_dataloader.dataset['eval'][idx][label_column]}\n")
                                    log_file.write("===================================================================\n")
                                except Exception as e:
                                    warnings.warn(f"Can't write config for prediction with idx {idx}\n"
                                                  f"Error message: {e}")
                                    pass
                            log_file.write(f"\n     Training arguments: \n")
                            for key, value in vars(training_args).items():
                                log_file.write(f"\n {key}: {value} ")
                    except Exception as e:
                        warnings.warn(f"Can't save config for this epoch {epoch}\n"
                                      f"Error message: {e}")

            del inference_model
            accelerator.print("Removing inference model offload_inf...")
            shutil.rmtree('offload_inf') if os.path.exists("offload_inf") else None
            gc.collect()

    save_push()


if __name__ == "__main__":
    train()