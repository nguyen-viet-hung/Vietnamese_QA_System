accelerate launch --config_file "src/models/configs/config_defaultMultiGPU.yaml" train.py \
        --lora_r 8 \
        --model_name_or_path distilgpt2 \
        --max_train_samples 8000 \
        --max_eval_samples 20 \
        --train_batch_size 4 \
        --val_file "src/data/features/final_storge_converted/yahma_alpaca-cleaned/AlpacaCleaned_translatedFormated.json" "src/data/features/final_storge_converted/yahma_alpaca-cleaned/AlpacaCleanedFormated.json" \
        --num_epochs 4 \
        --model_type CAUSAL_LM \
        --better_transformer \
        --gradient_accumulation_steps 32 \
        --eval_batch_size 2 \
        --lora_alpha 64 \
        --Optim_name PagedLion8bit \
        --enable_model_offload \
        --gradient_checkpointing \
        --use_8bit \
        --do_eval \
        --do_generate_eval \
        --merge_weight_eval \
        --llm_int8_enable_fp32_cpu_offload \
        --max_time 10 \
        --no_sample \
        --max_new_tokens 80
