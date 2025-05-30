for seed in 13   #You can change the sl to find the best hyperparameter.
do
    for sl in '7e-5' 
    do
          for loss_lambda in 0.1
          do
              for Prompt_pool_num in 7 8 9 10
              do
                  for num_image_tokens in 2 4
                  do
                   echo ${sl}
                   CUDA_VISIBLE_DEVICES=3 python MAESC_training_for_generated_dual_prompts_multitasks_Aspect.py \
                   --dataset twitter15 src/data/jsons/few_shot_for_prompt/twitter_2015/twitter15_${seed}_info.json \
                   --checkpoint_dir ./ \
                   --model_config data/bart-base/config.json \
                   --log_dir log_for_dual_prompts_multitasks_Aspect/15_${seed}_${sl}_${num_image_tokens}_aesc \
                   --num_beams 4 \
                   --eval_every 1 \
                   --lr ${sl} \
                   --batch_size 12 \
                   --epochs 70 \
                   --grad_clip 5 \
                   --warmup 0.1 \
                   --num_workers 8 \
                   --has_prompt \
                   --num_image_tokens ${num_image_tokens} \
                   --loss_lambda ${loss_lambda} \
                   --use_generated_aspect_prompt \
                   --use_different_aspect_prompt \
                   --use_multitasks \
                   --Prompt_Pool_num ${Prompt_pool_num} \
                   --seed ${seed}
                   done
               done
          done
     done
done