from collections import defaultdict
from typing import Optional, Tuple
from fastNLP.modules.torch.encoder import Seq2SeqEncoder
from fastNLP.modules.torch.decoder import Seq2SeqDecoder
from fastNLP.modules.torch import State
import torch
import torch.nn.functional as F
from torch import nn
from torch.nn.functional import cosine_similarity

from src.model.model_completion import ModalCompletionModule
from src.model.modeling_bart import (PretrainedBartModel, BartEncoder,
                                     BartDecoder, BartModel,
                                     BartClassificationHead,
                                     _make_linear_from_emb,
                                     _prepare_bart_decoder_inputs)
from transformers import BartTokenizer

from src.model.config import MultiModalBartConfig
# from src.model.mixins import GenerationMixin, FromPretrainedMixin
from src.model.modules_for_prompt import MultiModalBartEncoder, MultiModalBartDecoder_span, Span_loss, \
    MultiModalBartEncoder_for_Generating_sentiment_prompt, MultiModalBartDecoder_generate_sentiment_prompt, \
    MultiModalBartDecoder_MLM


class MultiModalBartModel_AESC(PretrainedBartModel):
    def build_model(self,
                    args,
                    bart_model,
                    tokenizer,
                    label_ids,
                    config,
                    decoder_type=None,
                    copy_gate=False,
                    use_encoder_mlp=False,
                    use_recur_pos=False,
                    tag_first=False):
        if args.bart_init:
            model = BartModel.from_pretrained(bart_model)
            num_tokens, _ = model.encoder.embed_tokens.weight.shape
            print('num_tokens', num_tokens)
            # 在原先的Bart tokenizer的基础上，增加模型需要的特殊Token
            model.resize_token_embeddings(
                len(tokenizer.unique_no_split_tokens) + num_tokens)
            encoder = model.encoder
            decoder = model.decoder
            # 编码器的 填充Token
            padding_idx = config.pad_token_id
            encoder.embed_tokens.padding_idx = padding_idx

            # if use_recur_pos:
            #     decoder.set_position_embedding(label_ids[0], tag_first)

            # --- Freeze BART Encoder and Decoder Parameters ---  <-- 添加参数冻结代码!

            # for param in encoder.parameters():  # 冻结 Encoder 所有参数
            #     param.requires_grad = False
            # for param in decoder.parameters():  # 冻结 Decoder 所有参数
            #     param.requires_grad = False
            # print("Froze BART encoder parameters!")  # 打印冻结信息

            _tokenizer = BartTokenizer.from_pretrained(bart_model)
            # 1. 计算token在扩展词表中的位置
            # 2. 基于基础BART词表生成融合嵌入
            # 3. 更新decoder嵌入矩阵
            for token in tokenizer.unique_no_split_tokens:
                if token[:2] == '<<':  # 特殊字符
                    index = tokenizer.convert_tokens_to_ids(
                        tokenizer._base_tokenizer.tokenize(token))
                    if len(index) > 1:
                        raise RuntimeError(f"{token} wrong split")
                    else:
                        index = index[0]
                    assert index >= num_tokens, (index, num_tokens, token)
                    indexes = _tokenizer.convert_tokens_to_ids(
                        _tokenizer.tokenize(token[2:-2]))
                    embed = model.encoder.embed_tokens.weight.data[indexes[0]]
                    for i in indexes[1:]:
                        embed += model.decoder.embed_tokens.weight.data[i]
                    embed /= len(indexes)
                    model.decoder.embed_tokens.weight.data[index] = embed
        else:
            raise RuntimeError("error init!!!!!!!")

        multimodal_encoder_for_generated_senti_prompt = MultiModalBartEncoder(config, encoder,
                                                                              tokenizer.img_feat_id,
                                                                              tokenizer.cls_token_id,
                                                                              args.num_image_tokens,
                                                                              tokenizer.pad_token_id)

        multimodal_encoder = MultiModalBartEncoder_for_Generating_sentiment_prompt(
            use_generated_prompt=args.use_generated_prompt,
            config=config,
            encoder=encoder,
            img_feat_id=tokenizer.img_feat_id,
            aspect_prompt_token_id=tokenizer.aspect_prompt_token_id,
            senti_prompt_token_id=tokenizer.senti_prompt_token_id,
            cls_token_id=tokenizer.cls_token_id,
            num_image_tokens=args.num_image_tokens,
            use_different_senti_prompt=args.use_different_senti_prompt,
            prompt_pool_num=args.Prompt_Pool_num,
            diversity_loss_weight=args.diversity_loss_weight,
            l2_reg_weight=args.l2_reg_weight
        )
        # (图像表示变成文本嵌入基础编码器, 情感提示编码器, BART的共享解码器)
        return (multimodal_encoder_for_generated_senti_prompt, multimodal_encoder, decoder, encoder)

    def __init__(self, config: MultiModalBartConfig, args, bart_model,
                 tokenizer, label_ids):
        super().__init__(config)
        self.config = config
        self.tokenizer = tokenizer
        label_ids = sorted(label_ids)
        # (图像表示变成文本嵌入基础编码器, 情感提示编码器, 共享解码器)

        multimodal_encoder_for_generated_senti_prompt, multimodal_encoder, share_decoder, encoder = self.build_model(
            args, bart_model, self.tokenizer, label_ids, config)
        causal_mask = torch.zeros(512, 512).fill_(float('-inf'))
        self.causal_mask = causal_mask.triu(diagonal=1)
        self.num_image_tokens = args.num_image_tokens

        self.senti_prompt_encoder = multimodal_encoder_for_generated_senti_prompt
        self.encoder = multimodal_encoder
        self.bart_encoder = encoder
        only_sc = False
        # need_tag = True  #if predict the sentiment or not
        if args.task == 'twitter_ae':
            need_tag = False
        else:
            need_tag = True
            # if args.task == 'twitter_sc':
            #     only_sc = True
        # 这个的decoder 是 用于特定于情绪的任务使用的解码器
        self.senti_prompt_decoder = MultiModalBartDecoder_generate_sentiment_prompt(self.config, share_decoder,
                                                                                    args.Prompt_Pool_num,
                                                                                    args.diversity_loss_weight,
                                                                                    args.l2_reg_weight)
        # 这个部分是整个BART 使用的 Decoder 这里会生成得所有的预测信息
        self.decoder = MultiModalBartDecoder_span(self.config,
                                                  self.tokenizer,
                                                  share_decoder,
                                                  self.tokenizer.pad_token_id,
                                                  label_ids,
                                                  self.causal_mask,
                                                  need_tag=need_tag,
                                                  only_sc=False)
        self.span_loss_fct = Span_loss()
        self.mlm_loss_module = MultiModalBartDecoder_MLM(self.config, self.senti_prompt_decoder.decoder)
        # 字幕与文本的相关度计算
        self.text_caption_cross_attention = nn.MultiheadAttention(embed_dim=self.config.hidden_size, num_heads=8,
                                                                  batch_first=True, dropout=0.2)
        # self.text_caption_attn_output_projection = nn.Linear(self.config.hidden_size, 1)

        self.text_caption_attn_output_projection = nn.Sequential(
            nn.Linear(768 + 1, 256),
            nn.LayerNorm(256),
            nn.GELU(),
            nn.Dropout(0.2),
            nn.Linear(256, 1),
        )  # 示例：将池化后的输出投影到 1 维

        # 字幕与文本的相关度计算 第二轮的encoder的
        self.text_caption_cross_attention_second = nn.MultiheadAttention(embed_dim=self.config.hidden_size, num_heads=8,
                                                                         batch_first=True, dropout=0.2)
        # self.text_caption_attn_output_projection_second = nn.Linear(self.config.hidden_size, 1)  # 示例：将池化后的输出投影到 1 维

        self.text_caption_attn_output_projection_second = nn.Sequential(
            nn.Linear(768 + 1, 256),
            nn.LayerNorm(256),
            nn.GELU(),
            nn.Dropout(0.2),
            nn.Linear(256, 1),
        )  # 示例：将池化后的输出投影到 1 维
        # 定义相关度阈值  可以 把阈值变成可学习的参数， 与固定的阈值做一个比较，
        if args.dataset[0][0] == 'twitter15':
            self.threshold = nn.Parameter(torch.tensor(0.7))
        elif args.dataset[0][0] == 'twitter17':
            self.threshold = nn.Parameter(torch.tensor(0.5))

        # 温度参数，用于调整sigmoid的陡峭程度
        self.temperature = nn.Parameter(torch.tensor(5.0))

        # 定义字幕名词和文本名词的相关度阈值， 当低于这个阈值的时候，同样可以认为， 图像与文本并不一致，说明图片信息将是干扰。
        if args.dataset[0][0] == 'twitter15':
            self.cosine_threshold = 0.9
        elif args.dataset[0][0] == 'twitter17':
            self.cosine_threshold = 0.9

        self.noun_cache = defaultdict(lambda: None)  # 名词嵌入缓存
        # 名词嵌入修正部分：
        self.nouns_cross_attention = nn.MultiheadAttention(768, 4, batch_first=True, dropout=0.1)
        self.gate_proj_nouns = nn.Linear(768 * 2, 1)

        self.nouns_cross_attention_image = nn.MultiheadAttention(768, 4, batch_first=True, dropout=0.1)
        self.gate_proj_nouns_image = nn.Linear(768 * 2, 1)

        # 添加模态补全模块
        self.modal_completion = ModalCompletionModule(hidden_size=config.d_model, max_length=20)
        self.use_caption_completion = False  # 默认启用字幕补全
        self.use_image_completion = False  # 默认禁用图像补全

        self.dropout_rate = 0.2  # 可调整的dropout率
        self.feature_dropout = nn.Dropout(self.dropout_rate)
        self.l2_reg_weight = 0.00005  # L2正则化权重

    # 新增：计算每个字幕名词与文本名词的相关性
    def get_bart_word_embedding(self, tokenizer, encoder, word, device):
        # 单个词编码，取非pad部分的平均（通常只有一个token）
        inputs = tokenizer(word, return_tensors='pt', add_special_tokens=False).to(device)
        with torch.no_grad():
            outputs = encoder(**inputs)
            last_hidden = outputs[0].squeeze(0)  # (seq_len, hidden)
            attention_mask = inputs['attention_mask'].squeeze(0)
            # print("w", last_hidden.size(), attention_mask.size())
            valid_hidden = last_hidden[attention_mask.bool()]
            if valid_hidden.shape[0] == 0:
                return torch.zeros(768, device=device)
            return valid_hidden.mean(dim=0)

    def prepare_state(self,
                      input_ids,
                      image_features,
                      attention_mask=None,
                      aesc_infos=None,
                      aspects_num=None,
                      sentence_mask=None,
                      image_mask=None,
                      mlm_message=None,
                      image_caption_valid=None,
                      image_caption_mask=None,
                      score=None,
                      caption_nouns=None,
                      sentence_nouns=None,
                      first=None):
        ##generate prompt for each instance
        # ResNet给出的图像表示会放在Prompt的最前面，所以后面的长度固定的情况下，可以指定特殊标记的位置了
        # bos & eos 用于指定 数据集中 文本模态的嵌入表示 位置。
        prompt_attention_mask = attention_mask

        if self.num_image_tokens == 0:
            end_index = 62
            begin_index = 22
        elif self.num_image_tokens == 1:
            end_index = 63
            begin_index = 23
        elif self.num_image_tokens == 2:
            end_index = 64
            begin_index = 24
        elif self.num_image_tokens == 3:
            end_index = 65
            begin_index = 25
        elif self.num_image_tokens == 4:
            end_index = 66
            begin_index = 26
        elif self.num_image_tokens == 5:
            end_index = 67
            begin_index = 27
        elif self.num_image_tokens == 6:
            end_index = 68
            begin_index = 28
        elif self.num_image_tokens == 7:
            end_index = 69
            begin_index = 29

        for i in range(len(prompt_attention_mask)):
            mask = prompt_attention_mask[i]
            mask[begin_index:end_index] = torch.zeros_like(mask[begin_index:end_index])  ##26:66 是aspect提示的位置
            prompt_attention_mask[i] = mask

        # 一开始的encoder因为方面术语Prompt部分的信息并没有获取，所以直接使用会干扰模型 所以本部分就需要先把这个部分去除
        # 而只要计算完了aspect_prompt
        dict_for_prompt = self.senti_prompt_encoder(input_ids=input_ids,
                                                    image_features=image_features,
                                                    attention_mask=prompt_attention_mask,
                                                    output_hidden_states=True,
                                                    sentence_mask=sentence_mask,
                                                    return_dict=True)
        # print("仅有图像特征输入的情况下的编码结果", dict_for_prompt.last_hidden_state.shape)

        #  通过计算字幕与文本的相关度，让图片特征的信息相应的减少
        # print("image_caption_mask:", image_caption_mask)
        image_caption_mask_expanded_for_image = image_caption_mask.unsqueeze(-1).to(
            attention_mask.device)  # 为图像 mask 扩展维度
        image_caption_embeddings = dict_for_prompt.last_hidden_state * image_caption_mask_expanded_for_image.float()  # 提取图像嵌入
        image_caption = image_caption_embeddings.to(attention_mask.device)  # [s, b, 768]  图像字幕嵌入，准备作为 Key/Value

        # 文本嵌入
        sentence_mask_expanded_for_image = sentence_mask.unsqueeze(-1).to(attention_mask.device)  # 为图像 mask 扩展维度
        text_embeddings = dict_for_prompt.last_hidden_state * sentence_mask_expanded_for_image.float()  # 提取图像嵌入
        text = text_embeddings.to(attention_mask.device)  # [s, b, 768]  图像字幕嵌入，准备作为 Key/Value
        # 图像嵌入
        image_mask_expanded_for_image = image_mask.unsqueeze(-1).to(attention_mask.device)  # 为图像 mask 扩展维度
        image_embeddings = dict_for_prompt.last_hidden_state * image_mask_expanded_for_image.float()  # 提取图像嵌入
        image = image_embeddings.to(attention_mask.device)  # [s, b, 768]  图像字幕嵌入，准备作为 Key/Value
        relevance_scores = torch.zeros(image_caption_mask.size(0), 1, device=input_ids.device)  # 初始化相关度得分张量 (形状 [b, 1])
        # print("image_caption_valid 维度", image_caption_mask.size())
        # print("图片特征长度", len(image_features))
        loss_crd = torch.tensor(0.0, dtype=torch.float)

        batch_size = image_caption_mask.size(0)
        # 使用 logits 计算损失，使用 scores 进行门控
        crd_logits_all = torch.zeros(batch_size, 1, device=attention_mask.device)  # 默认logit为0，对应sigmoid(0)=0.5
        # relevance_scores_all = torch.full((batch_size, 1), 0.5, device=attention_mask.device)  # 默认得分0.5 (或根据你的策略设为0.0或1.0)
        relevance_scores_all = score.unsqueeze(-1).to(attention_mask.device)  # 对于没有字幕就用预训练模型给出的参数

        # ------
        # 因为有些图像的字幕无法得到合理的信息，会造成部分缺失， 考虑用文本或说图像信息来辅助生成字幕。先暂时用线性层得到，或者说借助
        # MPLMM之前学习的
        # 创建原始掩码的副本，用于第二轮计算
        caption_completion_loss = torch.tensor(0.0, device=attention_mask.device)
        original_caption_valid = image_caption_valid.clone() if image_caption_valid is not None else None
        original_caption_mask = image_caption_mask.clone() if image_caption_mask is not None else None
        seq_len = image_caption_mask.size(1)
        caption_completion_loss = torch.tensor(0.0, device=attention_mask.device)
        # 好像补全没啥用，233.。
        if self.use_caption_completion:
            # 检测缺失的字幕
            caption_missing = ~image_caption_valid if image_caption_valid is not None else torch.zeros(batch_size,
                                                                                                       dtype=torch.bool,
                                                                                                       device=attention_mask.device)
            num_missing = caption_missing.sum().item()

            # if num_missing > 0:
            #     # 提取文本表示用于生成字幕
            #     # 为缺失字幕的样本生成补全
            #     completed_captions, quality_scores, target_length, begin_caption_emb, end_caption_emb = self.modal_completion.complete_caption(
            #         encoder_hidden_states=dict_for_prompt.last_hidden_state,
            #         attention_mask=attention_mask,
            #         sentence_mask=sentence_mask,
            #         image_mask=image_mask,
            #         image_caption_mask=image_caption_mask,
            #         image_caption_valid=image_caption_valid,
            #         relevance_score=score.unsqueeze(-1) if score is not None else None,
            #         threshold=self.threshold
            #     )
            #
            #     # 对于缺失字幕的样本，使用生成的字幕特征
            #     for i in range(batch_size):
            #         if caption_missing[i]:
            #             # 找到适合插入字幕的位置
            #             # 通常在图像之后，文本之前
            #             image_indices = torch.where(image_mask[i] > 0)[0]
            #             if len(image_indices) > 0:
            #                 insert_pos = image_indices[-1].item() + 1 + 1  # 同样的image_mask不包含特殊标记，多走一步
            #             else:
            #                 insert_pos = 2  # 不存在的话就是只有image_token的两个特殊标记
            #             # 下一个部分 Prompt 的起始位置 就是字幕的结束位置
            #             end_pos = begin_index
            #
            #             # 确保不超过序列长度
            #             seq_len = dict_for_prompt.last_hidden_state.size(1)
            #             end_pos = min(end_pos, seq_len)
            #
            #             # 计算可用空间
            #             available_space = end_pos - insert_pos
            #
            #             # 确保有足够空间放置字幕和特殊标记
            #             if available_space >= 2:  # 至少需要放置开始和结束标记
            #                 # 计算实际可用于字幕内容的长度
            #                 content_length = min(target_length, available_space - 2)
            #
            #                 # 更新字幕掩码
            #                 # 首先清除该区域的所有掩码
            #                 image_caption_mask[i, insert_pos:end_pos] = 0
            #
            #                 # 设置字幕内容的掩码 (注意：特殊标记不包含在掩码中)
            #                 image_caption_mask[i, insert_pos + 1:insert_pos + 1 + content_length] = 1
            #
            #                 # 更新有效标志
            #                 if image_caption_valid is not None:
            #                     image_caption_valid[i] = True
            #
            #                 # 将补全的字幕特征放入编码器输出中
            #                 # 放置开始标记 (掩码前一个位置)
            #                 dict_for_prompt.last_hidden_state[i, insert_pos] = begin_caption_emb[0]
            #
            #                 # 放置字幕内容 (掩码覆盖的位置)
            #                 dict_for_prompt.last_hidden_state[i,
            #                 insert_pos + 1:insert_pos + 1 + content_length] = completed_captions[i, :content_length]
            #
            #                 # 放置结束标记 (掩码后一个位置)
            #                 dict_for_prompt.last_hidden_state[i, insert_pos + 1 + content_length] = end_caption_emb[0]

            # 为所有样本生成补全（无论是否有字幕）
            completed_captions, quality_scores, target_length, begin_caption_emb, end_caption_emb = self.modal_completion.complete_caption(
                encoder_hidden_states=dict_for_prompt.last_hidden_state,
                attention_mask=attention_mask,
                sentence_mask=sentence_mask,
                image_mask=image_mask,
                image_caption_mask=image_caption_mask,
                image_caption_valid=image_caption_valid,
                relevance_score=score.unsqueeze(-1) if score is not None else None,
                threshold=self.threshold
            )

            # 处理已有字幕的样本 - 计算损失用于训练
            if self.training:
                for i in range(batch_size):
                    if image_caption_valid is not None and image_caption_valid[i] and not caption_missing[i]:
                        # 提取原始字幕嵌入用于计算损失
                        caption_indices = torch.where(image_caption_mask[i] > 0)[0]
                        if len(caption_indices) > 0:
                            # 找到字幕的起始和结束位置
                            caption_start = caption_indices[0].item()
                            caption_end = caption_indices[-1].item() + 1

                            # 计算生成字幕与原始字幕之间的损失
                            # 注意：需要处理长度不匹配的情况
                            orig_len = caption_end - caption_start
                            gen_len = min(target_length, completed_captions.size(1))
                            match_len = min(orig_len, gen_len)

                            if match_len > 0:
                                # 计算MSE损失
                                mse_loss = F.mse_loss(
                                    completed_captions[i, :match_len],
                                    dict_for_prompt.last_hidden_state[i, caption_start:caption_start + match_len]
                                )

                                # 计算余弦相似度损失
                                cos_sim = F.cosine_similarity(
                                    completed_captions[i, :match_len].view(match_len, -1),
                                    dict_for_prompt.last_hidden_state[i, caption_start:caption_start + match_len].view(
                                        match_len, -1),
                                    dim=1
                                ).mean()
                                cos_loss = 1 - cos_sim

                                # 组合损失
                                sample_loss = mse_loss * 0.5 + cos_loss * 0.5
                                caption_completion_loss += sample_loss
                                # print("loss", mse_loss, cos_loss, caption_completion_loss)
                                if self.verbose:
                                    print(
                                        f"样本 {i} 字幕重建: 原始长度={orig_len}, 生成长度={gen_len}, 匹配长度={match_len}, 损失={sample_loss.item():.4f}")

            # 对于缺失字幕的样本，使用生成的字幕特征
            if num_missing > 0:
                for i in range(batch_size):
                    if caption_missing[i]:
                        # 找到适合插入字幕的位置
                        # 通常在图像之后，文本之前
                        image_indices = torch.where(image_mask[i] > 0)[0]
                        if len(image_indices) > 0:
                            insert_pos = image_indices[-1].item() + 1 + 1  # 同样的image_mask不包含特殊标记，多走一步
                        else:
                            insert_pos = 2  # 不存在的话就是只有image_token的两个特殊标记
                        # 下一个部分 Prompt 的起始位置 就是字幕的结束位置
                        end_pos = begin_index

                        # 确保不超过序列长度
                        seq_len = dict_for_prompt.last_hidden_state.size(1)
                        end_pos = min(end_pos, seq_len)

                        # 计算可用空间
                        available_space = end_pos - insert_pos

                        # 确保有足够空间放置字幕和特殊标记
                        if available_space >= 2:  # 至少需要放置开始和结束标记
                            # 计算实际可用于字幕内容的长度
                            content_length = min(target_length, available_space - 2)

                            # 更新字幕掩码
                            # 首先清除该区域的所有掩码
                            image_caption_mask[i, insert_pos:end_pos] = 0

                            # 设置字幕内容的掩码 (注意：特殊标记不包含在掩码中)
                            image_caption_mask[i, insert_pos + 1:insert_pos + 1 + content_length] = 1

                            # 更新有效标志
                            if image_caption_valid is not None:
                                image_caption_valid[i] = True

                            # 将补全的字幕特征放入编码器输出中
                            # 放置开始标记 (掩码前一个位置)
                            dict_for_prompt.last_hidden_state[i, insert_pos] = begin_caption_emb[0]

                            # 放置字幕内容 (掩码覆盖的位置)
                            dict_for_prompt.last_hidden_state[i,
                            insert_pos + 1:insert_pos + 1 + content_length] = completed_captions[i, :content_length]

                            # 放置结束标记 (掩码后一个位置)
                            dict_for_prompt.last_hidden_state[i, insert_pos + 1 + content_length] = end_caption_emb[0]

        # ------
        # 全部都是有字幕的
        temp_caption_valid = image_caption_valid.clone()
        temp_caption_mask = image_caption_mask.clone()
        # 保留原始字幕信息
        image_caption_valid = original_caption_valid
        image_caption_mask = original_caption_mask
        valid_indices = torch.where(image_caption_valid)[0]
        # print("valid_indices", valid_indices.size())
        # print("score", score.size())
        num_valid = len(valid_indices)
        if num_valid > 0:
            # 2. 提取有效样本的数据
            valid_text_emb = text[valid_indices].to(attention_mask.device)  # [num_valid, seq_len_text, hidden_size]
            valid_caption_emb = image_caption[valid_indices].to(
                attention_mask.device)  # [num_valid, max_len, hidden_size]
            valid_text_mask = sentence_mask[valid_indices].to(attention_mask.device)  # [num_valid, max_len]
            # **关键: MultiheadAttention 的 key_padding_mask 需要 True 表示 Padding 位置**
            # 假设你的 mask 是 1 表示有效, 0 表示 padding, 需要转换
            valid_caption_padding_mask = (
                    image_caption_mask[valid_indices] == 0).to(
                attention_mask.device)  # [num_valid, max_len], True for padding

            # 3. 批处理交叉注意力 (假设 batch_first=True)
            # query: text, key/value: caption
            attn_output_valid, attn_weights = self.text_caption_cross_attention(
                query=valid_text_emb,
                key=valid_caption_emb,
                value=valid_caption_emb,
                key_padding_mask=valid_caption_padding_mask  # ****** 提供正确的 Mask ******
            )  # Output: [num_valid, max_len, hidden_size]

            # 提取最高的几个注意力权重，表示最相关的部分
            top_k_weights, _ = torch.topk(attn_weights, k=min(5, attn_weights.size(-1)), dim=-1)
            attention_focus = top_k_weights.mean(dim=-1).mean(dim=-1).unsqueeze(1)  # [num_valid, 1]

            # 4. 批处理屏蔽平均池化
            # 将 padding 位置的 attention output 置零
            attn_output_valid_masked = attn_output_valid * valid_text_mask.unsqueeze(-1).float().to(
                attention_mask.device)
            # 计算每个样本的有效长度
            text_lengths_valid = valid_text_mask.sum(dim=1, keepdim=True).float().to(
                attention_mask.device)  # [num_valid, 1]
            # 对有效位置求和
            sum_attn_output_valid = attn_output_valid_masked.sum(dim=1).to(
                attention_mask.device)  # [num_valid, hidden_size]
            # 计算平均值，防止除零
            mean_attn_output_valid = sum_attn_output_valid / torch.clamp(text_lengths_valid,
                                                                         min=1e-9)  # [num_valid, hidden_size]

            # 计算文本和图像表示的余弦相似度
            text_pooled = valid_text_emb.sum(dim=1) / torch.clamp(valid_text_mask.sum(dim=1, keepdim=True), min=1e-9)
            caption_pooled = valid_caption_emb.sum(dim=1) / torch.clamp((~valid_caption_padding_mask).sum(dim=1, keepdim=True), min=1e-9)
            cosine_sim = F.cosine_similarity(text_pooled, caption_pooled, dim=1, eps=1e-8).unsqueeze(1)  # [num_valid, 1]

            # 5. 多特征融合
            # 将注意力焦点和余弦相似度与平均表示拼接
            combined_features = torch.cat([
                mean_attn_output_valid,  # 交叉注意力特征
                cosine_sim,  # 余弦相似度特征
            ], dim=1)
            # 应用特征dropout
            if self.training:
                combined_features = self.feature_dropout(combined_features)
            # 5. 批处理线性投影，得到 Logits
            logits_valid = self.text_caption_attn_output_projection(combined_features)  # [num_valid, 1]

            # 6. 计算有效样本的相关性得分 (用于门控)
            scores_valid = torch.sigmoid(logits_valid).to(attention_mask.device)  # [num_valid, 1]

            # 7. 计算 CRD 损失 (仅针对有效样本)
            target_labels_valid = score[valid_indices].unsqueeze(1).to(attention_mask.device)  # [num_valid, 1]
            # 再转为硬标签
            hard_labels = (target_labels_valid > self.threshold).float()

            criterion_crd = nn.BCELoss()
            loss_crd_hard = criterion_crd(scores_valid, hard_labels)
            # 就正常的使用软标签
            loss_crd_soft = F.binary_cross_entropy(scores_valid, target_labels_valid)  # 无需阈值处理
            loss_crd = 0.9 * loss_crd_soft + 0.1 * loss_crd_hard
            # 8. 更新完整批次的 logits 和 scores
            # 使用 scatter_ 或 index_put_ 更新特定索引的值
            crd_logits_all[valid_indices] = logits_valid
            relevance_scores_all[valid_indices] = scores_valid

        # 恢复补全后的掩码 保证全部都有字幕，来计算
        image_caption_valid = temp_caption_valid
        image_caption_mask = temp_caption_mask
        # 对补全的字幕计算相关度但不参与梯度
        if self.use_caption_completion:
            with torch.no_grad():
                # 找出补全的字幕索引
                completed_indices = torch.where(image_caption_valid & ~original_caption_valid)[0]
                if len(completed_indices) > 0:
                    # 使用相同的相关度计算逻辑，但不累积梯度
                    completed_text_emb = text[completed_indices].to(attention_mask.device)
                    completed_caption_emb = image_caption[completed_indices].to(attention_mask.device)
                    completed_text_mask = sentence_mask[completed_indices].to(attention_mask.device)
                    completed_caption_padding_mask = (image_caption_mask[completed_indices] == 0).to(attention_mask.device)

                    # 计算补全字幕的相关度
                    completed_attn_output, _ = self.text_caption_cross_attention(
                        query=completed_text_emb,
                        key=completed_caption_emb,
                        value=completed_caption_emb,
                        key_padding_mask=completed_caption_padding_mask
                    )

                    # 将 padding 位置的 attention output 置零
                    attn_output_valid_masked = completed_attn_output * completed_text_mask.unsqueeze(-1).float().to(
                        attention_mask.device)
                    # 计算每个样本的有效长度
                    text_lengths_valid = completed_text_mask.sum(dim=1, keepdim=True).float().to(
                        attention_mask.device)  # [num_valid, 1]
                    # 对有效位置求和
                    sum_attn_output_valid = attn_output_valid_masked.sum(dim=1).to(
                        attention_mask.device)  # [num_valid, hidden_size]
                    # 计算平均值，防止除零
                    mean_attn_output_valid = sum_attn_output_valid / torch.clamp(text_lengths_valid,
                                                                                 min=1e-9)  # [num_valid, hidden_size]

                    # 计算文本和图像表示的余弦相似度
                    text_pooled = completed_text_emb.sum(dim=1) / torch.clamp(completed_text_mask.sum(dim=1, keepdim=True),
                                                                          min=1e-9)
                    caption_pooled = completed_caption_emb.sum(dim=1) / torch.clamp(
                        (~completed_caption_padding_mask).sum(dim=1, keepdim=True), min=1e-9)
                    cosine_sim = F.cosine_similarity(text_pooled, caption_pooled, dim=1, eps=1e-8).unsqueeze(
                        1)  # [num_valid, 1]

                    # 5. 多特征融合
                    # 将注意力焦点和余弦相似度与平均表示拼接
                    combined_features = torch.cat([
                        mean_attn_output_valid,  # 交叉注意力特征
                        cosine_sim,  # 余弦相似度特征
                    ], dim=1)
                    # 5. 批处理线性投影，得到 Logits
                    logits_valid = self.text_caption_attn_output_projection(combined_features)  # [num_valid, 1]

                    # 6. 计算有效样本的相关性得分 (用于门控)
                    scores_valid = torch.sigmoid(logits_valid).to(attention_mask.device)  # [num_valid, 1]

                    crd_logits_all[completed_indices] = logits_valid
                    relevance_scores_all[completed_indices] = scores_valid

        # 9. 使用相关性得分调整 image_features (向量化)
        # 确定广播维度，假设 image_features 是 [batch_size, num_patches, img_hidden]
        # 9. 使用相关性得分调整 image_features (向量化)
        # 应用学习型阈值和温度参数
        threshold = torch.sigmoid(self.threshold)
        temperature = torch.abs(self.temperature) + 1.0  # 确保温度参数为正

        # 平滑阈值处理
        adjusted_scores = torch.sigmoid((relevance_scores_all - threshold) * temperature)

        # 硬阈值+软权重结合
        hard_mask = (relevance_scores_all > adjusted_scores).float()
        gating_scores = relevance_scores_all * hard_mask
        gating_scores = gating_scores.unsqueeze(1).to(attention_mask.device)
        # gating_scores = relevance_scores_all.unsqueeze(1).to(attention_mask.device)  # 调整为 [batch_size, 1, 1] 以便广播

        # 或者如果 image_features 是 [batch_size, img_hidden]
        # gating_scores = relevance_scores_all # 调整为 [batch_size, 1]
        # print("gating_scores", gating_scores)
        # print("image_features", )
        for i in range(batch_size):
            image_features[i] = gating_scores[i] * image_features[i]

        weighted_image_tokens = image * gating_scores  # 逐元素相乘进行加权
        # print("image的维度", image.size())
        # print("计算的token结果", weighted_image_tokens.size())
        # print("dict_for_prompt.last_hidden_state", dict_for_prompt.last_hidden_state.size())
        encoder_outputs = dict_for_prompt.last_hidden_state
        mask_expanded_encoder = image_mask_expanded_for_image.repeat(1, 1, image.size(2)).to(attention_mask.device)
        dict_for_prompt.last_hidden_state = encoder_outputs.masked_scatter(
            mask_expanded_encoder,
            weighted_image_tokens[mask_expanded_encoder]
        )

        # 字幕修正
        weight_image_caption = image_caption * gating_scores
        caption_mask_expanded_encoder = image_caption_mask_expanded_for_image.repeat(1, 1, image.size(2)).to(attention_mask.device)
        dict_for_prompt.last_hidden_state = dict_for_prompt.last_hidden_state.masked_scatter(
            caption_mask_expanded_encoder,
            weight_image_caption[caption_mask_expanded_encoder]
        )

        # print("计算的 权重 ： ", relevance_weights)
        # TODO: 有个问题， 后续的字幕信息我们也一起作为了encoder的输入， 它的信息如何处理， 方案1、因为这两者 表达内容接近，可以采用一个门控机制学习
        # 2、直接认为这个字幕信息就是一个中间内容，计算出图片和文本的相关度后就可以丢弃了， 那为了保证结构统一，可以直接在此乘0，或是mask为0即可。

        # --------------
        # --------------------
        # 增加一个备选名词集合的部分，他将会作为候选的aspect，增强模型对方面术语的感受

        noun_embeds, noun_mask = self.process_batch(caption_nouns, sentence_nouns, attention_mask.device)

        # -------------------

        # 使用补全后的字幕进行后续处理
        prompt_decoder_input_ids, prompt_decoder_attention_mask = [
            aesc_infos['senti_prompt_decoder_input_ids'].to(input_ids.device),
            aesc_infos['senti_prompt_decoder_attention_mask'].to(input_ids.device)]
        # print("图像表示中使用的mask信息", prompt_attention_mask, prompt_decoder_input_ids,
        #       prompt_decoder_attention_mask)
        # 解码器使用的就是BART的解码器 至此完成模型SPD的部分，encoder_outputs 对应模型的E(m,s)部分 因为在MASC任务中只需要生成Ps提示即可。
        generated_prompt, diversity_loss, l2_reg_loss = self.senti_prompt_decoder(
            encoder_outputs=dict_for_prompt.last_hidden_state,  # 图像表示变换为文本表示后经过编码器得到的表示结果
            attention_mask=attention_mask,
            decoder_input_ids=prompt_decoder_input_ids, decoder_attention_mask=prompt_decoder_attention_mask,
            sentence_mask=sentence_mask,
            image_mask=image_mask,
            noun_embeds=noun_embeds,
            noun_mask=noun_mask,
            image_caption_valid=image_caption_valid,
            image_caption_mask=image_caption_mask
        )
        # 获取解码结果 [batch_size, seq_len, hidden_dim] 因为图像表示只会变成两个Token  所以这里的seq_len = 2
        # print("图像表示解码后的结果维度", generated_prompt.shape)

        generated_prompt = generated_prompt[:, 1:, :]  ##(batch_size, 2, 768)

        pseudo_loss = 0.0
        # 添加一个MLM损失，这个是一个伪CTTA方法，这个损失只在测试阶段使用，它的作用能迫使模型更好地理解测试数据的语言分布
        mlm_labels = mlm_message['mlm_labels'].to(input_ids.device)
        mlm_decoder_input_ids = mlm_message['mlm_decoder_input_ids'].to(input_ids.device)
        mlm_decoder_attention_mask = mlm_message['mlm_decoder_attention_mask'].to(input_ids.device)
        # mlm_inputs_id, mlm_labels = self.prepare_mlm(input_ids, attention_mask, self.tokenizer)
        pseudo_loss = self.mlm_loss_module(labels=mlm_labels, input_ids=input_ids,
                                           encoder_outputs=dict_for_prompt.last_hidden_state,
                                           attention_mask=attention_mask,
                                           decoder_input_ids=mlm_decoder_input_ids,
                                           decoder_attention_mask=mlm_decoder_attention_mask)

        # 至此已经组合得到了完整的Prompt ，需要再次经过Encoder进行编码 所以这里的编码只需要把需要组合的内容，一起拿到即可，所以generated_prompt 代表Ps
        # 这里同样做出修改，这里的图像嵌入，就需要用第一个encoder的结果使用了，不必重新计算一次。
        dict = self.encoder(input_ids=input_ids,
                            image_features=image_features,
                            attention_mask=attention_mask,
                            generated_prompt=generated_prompt,
                            aspects_num=aspects_num,
                            output_hidden_states=True,
                            sentence_mask=sentence_mask,
                            image_mask=image_mask,
                            encoder_outputs=encoder_outputs,
                            image_caption_valid=image_caption_valid,
                            image_caption_mask=image_caption_mask,
                            return_dict=True)
        # 这里把图像表示 ，情绪Prompt， 方面数量都给出，结合得到最后的用于模型BART的decoder生成的序列
        # 至此返回的是为图像表示和用于生成情绪Prompt信息的编码结果，第一项为最终层输出，第二项为每一层的输出
        # 其中的generated_prompt 是图像的编解码器结果，用他们得到情绪的embedding结果，并得到情绪的编码结果。

        # -------------- 在第二次的encoder结果中同样使用一样的修正部分，前面的修正部分，只为了aspect部分的信息获取。

        image_caption_mask_expanded_for_image = image_caption_mask.unsqueeze(-1).to(
            attention_mask.device)  # 为图像 mask 扩展维度
        image_caption_embeddings = dict.last_hidden_state * image_caption_mask_expanded_for_image.float()  # 提取图像嵌入
        image_caption = image_caption_embeddings.to(attention_mask.device)  # [s, b, 768]  图像字幕嵌入，准备作为 Key/Value

        # 文本嵌入
        sentence_mask_expanded_for_image = sentence_mask.unsqueeze(-1).to(attention_mask.device)  # 为图像 mask 扩展维度
        text_embeddings = dict.last_hidden_state * sentence_mask_expanded_for_image.float()  # 提取图像嵌入
        text = text_embeddings.to(attention_mask.device)  # [s, b, 768]  图像字幕嵌入，准备作为 Key/Value
        # 图像嵌入
        image_mask_expanded_for_image = image_mask.unsqueeze(-1).to(attention_mask.device)  # 为图像 mask 扩展维度
        image_embeddings = dict.last_hidden_state * image_mask_expanded_for_image.float()  # 提取图像嵌入
        image = image_embeddings.to(attention_mask.device)  # [s, b, 768]  图像字幕嵌入，准备作为 Key/Value
        relevance_scores = torch.zeros(image_caption_mask.size(0), 1, device=input_ids.device)  # 初始化相关度得分张量 (形状 [b, 1])
        # print("image_caption_valid 维度", image_caption_mask.size())
        # print("图片特征长度", len(image_features))
        loss_crd_second = torch.tensor(0.0, dtype=torch.float)
        valid_indices = torch.where(image_caption_valid)[0]
        # print("valid_indices", valid_indices.size())
        # print("score", score.size())
        num_valid = len(valid_indices)
        batch_size = image_caption_mask.size(0)
        # 使用 logits 计算损失，使用 scores 进行门控
        crd_logits_all = torch.zeros(batch_size, 1, device=attention_mask.device)  # 默认logit为0，对应sigmoid(0)=0.5
        # relevance_scores_all = torch.full((batch_size, 1), 0.5, device=attention_mask.device)  # 默认得分0.5 (或根据你的策略设为0.0或1.0)
        relevance_scores_all = score.unsqueeze(-1).to(attention_mask.device)  # 对于没有字幕就用预训练模型给出的参数
        if num_valid > 0:
            # 2. 提取有效样本的数据
            valid_text_emb = text[valid_indices].to(attention_mask.device)  # [num_valid, seq_len_text, hidden_size]
            valid_caption_emb = image_caption[valid_indices].to(
                attention_mask.device)  # [num_valid, max_len, hidden_size]
            valid_text_mask = sentence_mask[valid_indices].to(attention_mask.device)  # [num_valid, max_len]
            # **关键: MultiheadAttention 的 key_padding_mask 需要 True 表示 Padding 位置**
            # 假设你的 mask 是 1 表示有效, 0 表示 padding, 需要转换
            valid_caption_padding_mask = (
                    image_caption_mask[valid_indices] == 0).to(
                attention_mask.device)  # [num_valid, max_len], True for padding

            # 3. 批处理交叉注意力 (假设 batch_first=True)
            # query: text, key/value: caption
            attn_output_valid, _ = self.text_caption_cross_attention_second(
                query=valid_text_emb,
                key=valid_caption_emb,
                value=valid_caption_emb,
                key_padding_mask=valid_caption_padding_mask  # ****** 提供正确的 Mask ******
            )  # Output: [num_valid, max_len, hidden_size]

            # 4. 批处理屏蔽平均池化
            # 将 padding 位置的 attention output 置零
            attn_output_valid_masked = attn_output_valid * valid_text_mask.unsqueeze(-1).float().to(
                attention_mask.device)
            # 计算每个样本的有效长度
            text_lengths_valid = valid_text_mask.sum(dim=1, keepdim=True).float().to(
                attention_mask.device)  # [num_valid, 1]
            # 对有效位置求和
            sum_attn_output_valid = attn_output_valid_masked.sum(dim=1).to(
                attention_mask.device)  # [num_valid, hidden_size]
            # 计算平均值，防止除零
            mean_attn_output_valid = sum_attn_output_valid / torch.clamp(text_lengths_valid,
                                                                         min=1e-9)  # [num_valid, hidden_size]
            # 计算文本和图像表示的余弦相似度
            text_pooled = valid_text_emb.sum(dim=1) / torch.clamp(valid_text_mask.sum(dim=1, keepdim=True), min=1e-9)
            caption_pooled = valid_caption_emb.sum(dim=1) / torch.clamp(
                (~valid_caption_padding_mask).sum(dim=1, keepdim=True), min=1e-9)
            cosine_sim = F.cosine_similarity(text_pooled, caption_pooled, dim=1, eps=1e-8).unsqueeze(
                1)  # [num_valid, 1]

            # 多特征融合
            combined_features = torch.cat([
                mean_attn_output_valid,  # 交叉注意力特征
                cosine_sim,  # 余弦相似度特征
            ], dim=1)

            if self.training:
                combined_features = self.feature_dropout(combined_features)
            # 5. 批处理线性投影，得到 Logits
            logits_valid = self.text_caption_attn_output_projection_second(combined_features)  # [num_valid, 1]

            # 6. 计算有效样本的相关性得分 (用于门控)
            scores_valid = torch.sigmoid(logits_valid).to(attention_mask.device)  # [num_valid, 1]

            # 7. 计算 CRD 损失 (仅针对有效样本)
            target_labels_valid = score[valid_indices].unsqueeze(1).to(attention_mask.device)  # [num_valid, 1]
            # 再转为硬标签
            hard_labels = (target_labels_valid > self.threshold).float()

            criterion_crd = nn.BCELoss()
            loss_crd_hard_second = criterion_crd(scores_valid, hard_labels)
            # 就正常的使用软标签
            loss_crd_soft_second = F.binary_cross_entropy(scores_valid, target_labels_valid)  # 无需阈值处理
            loss_crd_second = 0.9 * loss_crd_soft_second + 0.1 * loss_crd_hard_second
            # 8. 更新完整批次的 logits 和 scores
            # 使用 scatter_ 或 index_put_ 更新特定索引的值
            crd_logits_all[valid_indices] = logits_valid
            relevance_scores_all[valid_indices] = scores_valid
        if self.use_caption_completion:
            with torch.no_grad():
                # 找出补全的字幕索引
                completed_indices = torch.where(image_caption_valid & ~original_caption_valid)[0]
                if len(completed_indices) > 0:
                    # 使用相同的相关度计算逻辑，但不累积梯度
                    completed_text_emb = text[completed_indices].to(attention_mask.device)
                    completed_caption_emb = image_caption[completed_indices].to(attention_mask.device)
                    completed_text_mask = sentence_mask[completed_indices].to(attention_mask.device)
                    completed_caption_padding_mask = (image_caption_mask[completed_indices] == 0).to(attention_mask.device)

                    # 计算补全字幕的相关度
                    completed_attn_output, _ = self.text_caption_cross_attention_second(
                        query=completed_text_emb,
                        key=completed_caption_emb,
                        value=completed_caption_emb,
                        key_padding_mask=completed_caption_padding_mask
                    )

                    # 将 padding 位置的 attention output 置零
                    attn_output_valid_masked = completed_attn_output * completed_text_mask.unsqueeze(-1).float().to(
                        attention_mask.device)
                    # 计算每个样本的有效长度
                    text_lengths_valid = completed_text_mask.sum(dim=1, keepdim=True).float().to(
                        attention_mask.device)  # [num_valid, 1]
                    # 对有效位置求和
                    sum_attn_output_valid = attn_output_valid_masked.sum(dim=1).to(
                        attention_mask.device)  # [num_valid, hidden_size]
                    # 计算平均值，防止除零
                    mean_attn_output_valid = sum_attn_output_valid / torch.clamp(text_lengths_valid,
                                                                                 min=1e-9)  # [num_valid, hidden_size]

                    # 计算文本和图像表示的余弦相似度
                    text_pooled = completed_text_emb.sum(dim=1) / torch.clamp(completed_text_mask.sum(dim=1, keepdim=True),
                                                                          min=1e-9)
                    caption_pooled = completed_caption_emb.sum(dim=1) / torch.clamp(
                        (~completed_caption_padding_mask).sum(dim=1, keepdim=True), min=1e-9)
                    cosine_sim = F.cosine_similarity(text_pooled, caption_pooled, dim=1, eps=1e-8).unsqueeze(
                        1)  # [num_valid, 1]

                    # 5. 多特征融合
                    # 将注意力焦点和余弦相似度与平均表示拼接
                    combined_features = torch.cat([
                        mean_attn_output_valid,  # 交叉注意力特征
                        cosine_sim,  # 余弦相似度特征
                    ], dim=1)
                    # 5. 批处理线性投影，得到 Logits
                    logits_valid = self.text_caption_attn_output_projection_second(combined_features)  # [num_valid, 1]

                    # 6. 计算有效样本的相关性得分 (用于门控)
                    scores_valid = torch.sigmoid(logits_valid).to(attention_mask.device)  # [num_valid, 1]

                    crd_logits_all[completed_indices] = logits_valid
                    relevance_scores_all[completed_indices] = scores_valid

        # 9. 使用相关性得分调整 image_features (向量化)
        # 确定广播维度，假设 image_features 是 [batch_size, num_patches, img_hidden]
        # 9. 使用相关性得分调整 image_features (向量化)
        # 应用学习型阈值和温度参数
        threshold = torch.sigmoid(self.threshold)
        temperature = torch.abs(self.temperature) + 1.0  # 确保温度参数为正

        # 平滑阈值处理
        adjusted_scores = torch.sigmoid((relevance_scores_all - threshold) * temperature)

        # 硬阈值+软权重结合
        hard_mask = (relevance_scores_all > adjusted_scores).float()

        # 在第二次的
        gating_scores = relevance_scores_all * hard_mask
        gating_scores = gating_scores.unsqueeze(1).to(attention_mask.device)
        # 确定广播维度，假设 image_features 是 [batch_size, num_patches, img_hidden]
        # gating_scores = relevance_scores_all.unsqueeze(1).to(attention_mask.device)  # 调整为 [batch_size, 1, 1] 以便广播
        # 图像特征后续不用了，就扔掉了。

        weighted_image_tokens = image * gating_scores  # 逐元素相乘进行加权
        # print("image的维度", image.size())
        # print("计算的token结果", weighted_image_tokens.size())
        # print("dict_for_prompt.last_hidden_state", dict_for_prompt.last_hidden_state.size())
        encoder_outputs = dict.last_hidden_state
        mask_expanded_encoder = image_mask_expanded_for_image.repeat(1, 1, image.size(2)).to(attention_mask.device)
        dict.last_hidden_state = encoder_outputs.masked_scatter(
            mask_expanded_encoder,
            weighted_image_tokens[mask_expanded_encoder]
        )
        # 字幕修正
        weight_image_caption = image_caption * gating_scores
        caption_mask_expanded_encoder = image_caption_mask_expanded_for_image.repeat(1, 1, image.size(2)).to(attention_mask.device)
        dict.last_hidden_state = dict.last_hidden_state.masked_scatter(
            caption_mask_expanded_encoder,
            weight_image_caption[caption_mask_expanded_encoder]
        )

        # ------ 名词修正同样来一次。
        # encoder_outputs = dict.last_hidden_state
        #
        # mask_expanded = sentence_mask.unsqueeze(-1).to(attention_mask.device)  # [b,s,1]
        # mask_expanded_encoder = mask_expanded.repeat(1, 1, encoder_outputs.size(2)).to(attention_mask.device)
        # # 使用广播机制提取文本特征 [b,s,768]
        # text_embeddings = encoder_outputs * mask_expanded.float()
        #
        # # --- 新增：图像嵌入交叉注意力计算 ---
        # # 现在利用相关度对 图像信息进行了修正， 其实可以把这个图像融合也放回来试试效果
        # image_mask_expanded_for_image = image_mask.unsqueeze(-1).to(attention_mask.device)  # 为图像 mask 扩展维度
        # image_embeddings = encoder_outputs * image_mask_expanded_for_image.float()  # 提取图像嵌入
        # image_mask_expanded_for_image = image_mask_expanded_for_image.repeat(1, 1, text_embeddings.size(2)).to(attention_mask.device)
        # # 再做任何操作前，先和名词做一个交叉注意力计算，让文本嵌入与更加关注特定的Aspect，也就是我们的目标
        # nouns_attn_output, attn_weights = self.nouns_cross_attention(
        #     query=text_embeddings,
        #     key=noun_embeds,
        #     value=noun_embeds,
        #     key_padding_mask=noun_mask,  # True 表示需要被掩码的位置
        #     need_weights=True  # 返回注意力权重以便调试
        # )
        # # 直接相加 or 门控融合
        # # text_embeddings = text_embeddings + nouns_attn_output
        #
        # gate_nouns = torch.sigmoid(self.gate_proj_nouns(torch.cat([text_embeddings, nouns_attn_output], dim=-1)))
        # text_embeddings = gate_nouns * text_embeddings + (1 - gate_nouns) * nouns_attn_output
        # # 同理对图像嵌入也做同样操作
        #
        # nouns_attn_output_image, attn_weights = self.nouns_cross_attention_image(
        #     query=image_embeddings,
        #     key=noun_embeds,
        #     value=noun_embeds,
        #     key_padding_mask=noun_mask,  # True 表示需要被掩码的位置
        #     need_weights=True  # 返回注意力权重以便调试
        # )
        # gate_nouns_image = torch.sigmoid(
        #     self.gate_proj_nouns_image(torch.cat([image_embeddings, nouns_attn_output_image], dim=-1)))
        # image_embeddings = gate_nouns_image * image_embeddings + (1 - gate_nouns_image) * nouns_attn_output_image
        # #  把计算结果放回去，
        # dict.last_hidden_state = encoder_outputs.masked_scatter(
        #     image_mask_expanded_for_image,
        #     image_embeddings[image_mask_expanded_for_image]
        # )
        # #  把计算结果放回去，
        # dict.last_hidden_state = dict.last_hidden_state.masked_scatter(
        #     mask_expanded_encoder,
        #     text_embeddings[mask_expanded_encoder]
        # )

        # --------------

        encoder_outputs = dict.last_hidden_state
        hidden_states = dict.hidden_states
        encoder_mask = attention_mask
        # print("num_token, end_index", self.num_image_tokens, end_index)
        src_embed_outputs = hidden_states[0]  # 第零层的输出。
        state = BartState(
            encoder_outputs,
            encoder_mask,
            input_ids[:,
            end_index:],  # the text features start from index 38, the front are image features.
            first,
            src_embed_outputs,
            end_index)  # 其封装函数见下方  这里做一个修改，源代码并没有特殊处理end_index的部分， 而是直接用64代替
        # 这只能处理image_token=2 的一种情况。故做出修改。
        # setattr(state, 'tgt_seq_len', tgt_seq_len)
        print("两次相关度计算的损失 以及生成字幕部分的损失", loss_crd.item(), loss_crd_second.item(), caption_completion_loss.item())
        loss_crd_all = loss_crd + loss_crd_second + caption_completion_loss
        return state, diversity_loss, l2_reg_loss, pseudo_loss, loss_crd_all

    def forward(
            self,
            input_ids,
            image_features,
            attention_mask=None,
            aesc_infos=None,
            aspects_num=None,
            sentence_mask=None,
            image_mask=None,
            mlm_message=None,
            image_caption_valid=None,
            image_caption_mask=None,
            score=None,
            caption_nouns=None,
            sentence_nouns=None,
            encoder_outputs: Optional[Tuple] = None,
            use_cache=None,
            output_attentions=None,
            output_hidden_states=None,
    ):
        ### for prompt
        # import ipdb; ipdb.set_trace()

        ## for aspect-spans  这里是ASC任务模型的运行部分：
        # 在单单情绪分析任务下， 方面数目和索引由数据集 直接给出
        aspects_num = torch.tensor(aspects_num).to(input_ids.device)
        state, diversity_loss, l2_reg_loss, pseudo_loss, loss_crd = self.prepare_state(input_ids, image_features,
                                                                                       attention_mask,
                                                                                       aesc_infos,
                                                                                       aspects_num,
                                                                                       sentence_mask, image_mask,
                                                                                       mlm_message,
                                                                                       image_caption_valid,
                                                                                       image_caption_mask, score,
                                                                                       caption_nouns,
                                                                                       sentence_nouns,
                                                                                       )
        spans, span_mask = [
            aesc_infos['labels'].to(input_ids.device),
            aesc_infos['masks'].to(input_ids.device)
        ]

        logits = self.decoder(spans, state)  ## spans: (2, 13) logits: (2, 12, 40)

        loss = self.span_loss_fct(spans[:, 1:], logits, span_mask[:, 1:])
        print("四个loss", loss.item(), diversity_loss.item(), l2_reg_loss.item(), loss_crd.item())

        all_loss = loss + diversity_loss + l2_reg_loss + loss_crd
        return all_loss, aspects_num, pseudo_loss  # 同样的 源代码少了一个参数， 因为ASC任务只检测情绪极性，所以aspect_num 直接使用结果即可。

    def _get_noun_embedding(self, noun: str) -> torch.Tensor:
        """将名词转换为嵌入向量

        Args:
            noun (str): 输入名词（如 "person", "object"）

        Returns:
            torch.Tensor: 嵌入向量 [hidden_size]
        """
        # Step 1: 分词
        noun_tokens = self.tokenizer._base_tokenizer.tokenize(noun, add_prefix_space=True)

        # Step 2: 处理空token（如特殊符号）
        if not noun_tokens:
            return torch.zeros(768, device=self.device)

        # Step 3: 转换为token IDs
        input_ids = self.tokenizer._base_tokenizer.convert_tokens_to_ids(noun_tokens)
        input_ids = torch.tensor([input_ids], device=self.device)  # [1, seq_len]

        # # Step 4: 截断超长序列
        # max_length = self.bart_encoder.config.max_position_embeddings
        # input_ids = input_ids[:, :max_length]

        # Step 5: 生成注意力掩码
        attention_mask = torch.ones_like(input_ids, device=self.device)

        # Step 6: 编码获取隐藏状态
        with torch.no_grad():
            outputs = self.bart_encoder(
                input_ids=input_ids,
                attention_mask=attention_mask
            )
            hidden_states = outputs[0]  # [1, seq_len, hidden_size]

        # Step 7: 池化（取均值）
        return hidden_states.mean(dim=1).squeeze(0)  # [hidden_size]

    # def batch_encode_nouns(self, noun_list, device):
    #     """批量编码名词"""
    #     unique_nouns = list({noun for sublist in noun_list for noun in sublist})
    #     inputs = self.tokenizer._base_tokenizer(
    #         unique_nouns,
    #         add_prefix_space=True,
    #         padding=True,
    #         return_tensors="pt"
    #     ).to(device)
    #
    #     with torch.no_grad():
    #         outputs = self.bart_encoder(**inputs)
    #         embeddings = outputs[0].mean(dim=1)
    #
    #     # 更新缓存
    #     for noun, emb in zip(unique_nouns, embeddings):
    #         self.noun_cache[noun] = emb.detach()
    #
    #     return {noun: self.noun_cache[noun] for noun in unique_nouns}
    #
    # def process_batch(self, caption_nouns, sentence_nouns, device):
    #     # 预加载默认名词嵌入
    #     for noun in ["person", "object"]:
    #         if noun not in self.noun_cache:
    #             self.noun_cache[noun] = self._get_noun_embedding(noun)
    #
    #     all_nouns = [cap + sent for cap, sent in zip(caption_nouns, sentence_nouns)]
    #     if all_nouns is not None:
    #         batch_embeddings = self.batch_encode_nouns(all_nouns, device)
    #     # 批量处理逻辑
    #     merged_nouns = []
    #     for cap_nouns, sent_nouns in zip(caption_nouns, sentence_nouns):
    #         # 从缓存获取嵌入
    #         cap_embs = [self.noun_cache[n] for n in cap_nouns if self.noun_cache[n] is not None]
    #         sent_embs = [self.noun_cache[n] for n in sent_nouns if self.noun_cache[n] is not None]
    #
    #         # 批量计算相似度矩阵
    #         if cap_embs and sent_embs:
    #             cap_matrix = torch.stack(cap_embs).to(device)
    #             sent_matrix = torch.stack(sent_embs).to(device)
    #             sim_matrix = cosine_similarity(
    #                 cap_matrix.unsqueeze(1),
    #                 sent_matrix.unsqueeze(0),
    #                 dim=-1
    #             )
    #             mean_sims = sim_matrix.mean(dim=1)
    #             valid_mask = mean_sims >= self.cosine_threshold
    #             keep_cap = [cap_nouns[i] for i, m in enumerate(valid_mask) if m]
    #         else:
    #             keep_cap = []
    #
    #         # 修正1：固定填充两个默认名词
    #         merged = list(set(keep_cap + sent_nouns))
    #         if not merged:
    #             merged = ["person", "object"]  # 直接赋值
    #
    #         merged_nouns.append(merged)
    #
    #     # 动态计算最大长度
    #     max_len = max(len(nouns) for nouns in merged_nouns) or 2  # 保证最小长度
    #
    #     # 修正2：安全初始化张量
    #     noun_embeds = torch.randn(  # 随机初始化代替全零
    #         len(merged_nouns), max_len, 768,
    #         device=device
    #     )
    #     noun_mask = torch.ones(
    #         len(merged_nouns), max_len,
    #         dtype=torch.bool, device=device
    #     )
    #
    #     # 修正3：带缓存的嵌入生成
    #     for i, nouns in enumerate(merged_nouns):
    #         current_embs = []
    #         for n in nouns:
    #             if n not in self.noun_cache:
    #                 self.noun_cache[n] = self._get_noun_embedding(n)
    #             current_embs.append(self.noun_cache[n])
    #
    #         if current_embs:
    #             stacked = torch.stack(current_embs)
    #             noun_embeds[i, :len(stacked)] = stacked
    #             noun_mask[i, :len(stacked)] = False
    #         else:  # 双重保障
    #             noun_embeds[i, 0] = self.noun_cache["person"]
    #             noun_mask[i, 0] = False
    #             noun_embeds[i, 1] = self.noun_cache["object"]
    #             noun_mask[i, 1] = False
    #
    #     return noun_embeds, noun_mask
    def batch_encode_nouns(self, all_nouns, device):
        """批量编码名词，确保安全处理空值"""
        # 检查输入是否为空
        if not all_nouns or all(not nouns for nouns in all_nouns):
            # 如果全部为空，返回默认名词的嵌入
            default_nouns = ["person", "object"]
            default_embeds = torch.stack([self.noun_cache[n] for n in default_nouns]).to(device)
            return default_embeds.unsqueeze(0)  # [1, 2, 768]

        # 收集所有唯一名词
        unique_nouns = set()
        for nouns in all_nouns:
            unique_nouns.update(nouns)

        # 确保默认名词在缓存中
        for noun in ["person", "object"]:
            if noun not in self.noun_cache:
                self.noun_cache[noun] = self._get_noun_embedding(noun)
            unique_nouns.add(noun)  # 确保默认名词在列表中

        unique_nouns = list(unique_nouns)

        # 安全检查：确保列表不为空
        if not unique_nouns:
            unique_nouns = ["person", "object"]

        try:
            # 批量编码所有唯一名词
            inputs = self.tokenizer._base_tokenizer(
                unique_nouns,
                add_prefix_space=True,
                padding=True,
                return_tensors="pt"
            ).to(device)

            with torch.no_grad():
                outputs = self.bart_encoder(**inputs)
                embeddings = outputs[0].mean(dim=1)  # [num_unique, hidden_size]

            # 更新缓存
            for noun, emb in zip(unique_nouns, embeddings):
                self.noun_cache[noun] = emb.detach().cpu()  # 存储在CPU上节省GPU内存

            # 构建批次嵌入
            batch_size = len(all_nouns)
            max_nouns = max(len(nouns) if nouns else 2 for nouns in all_nouns)  # 至少为2，容纳默认名词
            batch_embeddings = torch.zeros((batch_size, max_nouns, 768), device=device)

            for i, nouns in enumerate(all_nouns):
                if not nouns:  # 如果为空，使用默认名词
                    batch_embeddings[i, 0] = self.noun_cache["person"].to(device)
                    batch_embeddings[i, 1] = self.noun_cache["object"].to(device)
                else:
                    for j, noun in enumerate(nouns[:max_nouns]):
                        batch_embeddings[i, j] = self.noun_cache[noun].to(device)

            return batch_embeddings

        except Exception as e:
            print(f"批量编码名词时出错: {e}")
            # 出错时返回默认嵌入
            default_embeds = torch.stack([
                self.noun_cache["person"].to(device),
                self.noun_cache["object"].to(device)
            ])  # [2, 768]
            return default_embeds.unsqueeze(0).expand(len(all_nouns), 2, 768)  # [batch_size, 2, 768]

    def process_batch(self, caption_nouns, sentence_nouns, device):
        """处理一批名词，确保安全处理空值"""
        # 确保输入不为None
        if caption_nouns is None or sentence_nouns is None:
            # 使用默认名词
            batch_size = 1
            if caption_nouns is not None:
                batch_size = len(caption_nouns)
            elif sentence_nouns is not None:
                batch_size = len(sentence_nouns)

            # 创建默认嵌入和掩码
            for noun in ["person", "object"]:
                if noun not in self.noun_cache:
                    self.noun_cache[noun] = self._get_noun_embedding(noun)

            default_embeds = torch.stack([
                self.noun_cache["person"].to(device),
                self.noun_cache["object"].to(device)
            ])  # [2, 768]

            batch_embeds = default_embeds.unsqueeze(0).expand(batch_size, 2, 768)  # [batch_size, 2, 768]
            batch_mask = torch.zeros((batch_size, 2), dtype=torch.bool, device=device)  # 不掩码，全部可见
            return batch_embeds, batch_mask

        # 标准化输入，确保它们是列表的列表
        batch_size = len(caption_nouns)
        safe_caption_nouns = []
        safe_sentence_nouns = []

        for i in range(batch_size):
            cap_nouns = caption_nouns[i] if i < len(caption_nouns) and caption_nouns[i] is not None else []
            sent_nouns = sentence_nouns[i] if i < len(sentence_nouns) and sentence_nouns[i] is not None else []

            # 过滤掉None和空字符串
            cap_nouns = [n for n in cap_nouns if n is not None and n != ""]
            sent_nouns = [n for n in sent_nouns if n is not None and n != ""]

            safe_caption_nouns.append(cap_nouns)
            safe_sentence_nouns.append(sent_nouns)

        # 合并名词并确保默认名词在缓存中
        for noun in ["person", "object"]:
            if noun not in self.noun_cache:
                self.noun_cache[noun] = self._get_noun_embedding(noun)

        # 处理每个样本的名词
        merged_nouns = []
        for cap_nouns, sent_nouns in zip(safe_caption_nouns, safe_sentence_nouns):
            # 合并并去重
            merged = list(set(cap_nouns + sent_nouns))

            # 如果合并后为空，使用默认名词
            if not merged:
                merged = ["person", "object"]

            merged_nouns.append(merged)

        # 获取嵌入
        batch_embeddings = self.batch_encode_nouns(merged_nouns, device)

        # 创建掩码 (False表示有效位置，True表示需要被掩码的位置)
        max_nouns = batch_embeddings.size(1)
        batch_mask = torch.ones((batch_size, max_nouns), dtype=torch.bool, device=device)

        for i, nouns in enumerate(merged_nouns):
            # 有效位置设为False
            batch_mask[i, :min(len(nouns), max_nouns)] = False

        return batch_embeddings, batch_mask


class BartState(State):
    def __init__(self, encoder_output, encoder_mask, src_tokens, first,
                 src_embed_outputs, end_index):
        # 继承重排序部分，用于beam search进行序列生成时使用。重排序src_tokens, first,
        #                  src_embed_outputs 这三者的内容
        super().__init__(encoder_output, encoder_mask)
        self.past_key_values = None
        self.src_tokens = src_tokens
        self.first = first
        self.src_embed_outputs = src_embed_outputs
        self.end_index = end_index

    def reorder_state(self, indices: torch.LongTensor):
        super().reorder_state(indices)
        self.src_tokens = self._reorder_state(self.src_tokens, indices)
        if self.first is not None:
            self.first = self._reorder_state(self.first, indices)
        self.src_embed_outputs = self._reorder_state(self.src_embed_outputs,
                                                     indices)
        if self.past_key_values is not None:
            new = []
            for layer in self.past_key_values:
                new_layer = {}
                for key1 in list(layer.keys()):
                    new_layer_ = {}
                    for key2 in list(layer[key1].keys()):
                        if layer[key1][key2] is not None:
                            layer[key1][key2] = self._reorder_state(
                                layer[key1][key2], indices)
                            # print(key1, key2, layer[key1][key2].shape)
                        new_layer_[key2] = layer[key1][key2]
                    new_layer[key1] = new_layer_
                new.append(new_layer)
            self.past_key_values = new
