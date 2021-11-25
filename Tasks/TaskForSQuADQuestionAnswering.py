import sys

sys.path.append('../')
from model.BasicBert.BertConfig import BertConfig
from model.DownstreamTasks import BertForQuestionAnswering
from utils.data_helpers import LoadSQuADQuestionAnsweringDataset
from utils.log_helper import logger_init
from transformers import BertTokenizer
import logging
import torch
import os
import time
import numpy as np


class ModelConfig:
    def __init__(self):
        self.project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.dataset_dir = os.path.join(self.project_dir, 'data', 'SQuAD')
        self.pretrained_model_dir = os.path.join(self.project_dir, "bert_base_uncased_english")
        self.vocab_path = os.path.join(self.pretrained_model_dir, 'vocab.txt')
        self.device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
        self.train_file_path = os.path.join(self.dataset_dir, 'train1.json')
        self.test_file_path = os.path.join(self.dataset_dir, 'test1.json')
        self.model_save_dir = os.path.join(self.project_dir, 'cache')
        self.logs_save_dir = os.path.join(self.project_dir, 'logs')
        self.is_sample_shuffle = True
        self.batch_size = 16
        self.max_sen_len = None
        self.learning_rate = 5e-5
        self.epochs = 3
        self.model_val_per_epoch = 1
        logger_init(log_file_name='qa', log_level=logging.DEBUG,
                    log_dir=self.logs_save_dir)
        if not os.path.exists(self.model_save_dir):
            os.makedirs(self.model_save_dir)

        # 把原始bert中的配置参数也导入进来
        bert_config_path = os.path.join(self.pretrained_model_dir, "config.json")
        bert_config = BertConfig.from_json_file(bert_config_path)
        for key, value in bert_config.__dict__.items():
            self.__dict__[key] = value
        # 将当前配置打印到日志文件中
        logging.info(" ### 将当前配置打印到日志文件中 ")
        for key, value in self.__dict__.items():
            logging.info(f"### {key} = {value}")


def train(config):
    model = BertForQuestionAnswering(config,
                                     config.pretrained_model_dir)
    model_save_path = os.path.join(config.model_save_dir, 'model.pt')
    if os.path.exists(model_save_path):
        loaded_paras = torch.load(model_save_path)
        model.load_state_dict(loaded_paras)
        logging.info("## 成功载入已有模型，进行追加训练......")
    model = model.to(config.device)
    optimizer = torch.optim.Adam(model.parameters(), lr=config.learning_rate)
    model.train()
    bert_tokenize = BertTokenizer.from_pretrained(config.pretrained_model_dir).tokenize
    data_loader = LoadSQuADQuestionAnsweringDataset(vocab_path=config.vocab_path,
                                                    tokenizer=bert_tokenize,
                                                    batch_size=config.batch_size,
                                                    max_sen_len=config.max_sen_len,
                                                    max_position_embeddings=config.max_position_embeddings,
                                                    pad_index=config.pad_token_id,
                                                    is_sample_shuffle=config.is_sample_shuffle)
    train_iter, test_iter, val_iter = \
        data_loader.load_train_val_test_data(train_file_path=config.train_file_path,
                                             test_file_path=config.test_file_path,
                                             only_test=False)
    max_acc = 0
    for epoch in range(config.epochs):
        losses = 0
        start_time = time.time()
        for idx, (batch_input, batch_seg, batch_label) in enumerate(train_iter):
            batch_input = batch_input.to(config.device)  # [src_len, batch_size]
            batch_seg = batch_seg.to(config.device)
            batch_label = batch_label.to(config.device)
            padding_mask = (batch_input == data_loader.PAD_IDX).transpose(0, 1)
            loss, start_logits, end_logits = model(input_ids=batch_input,
                                                   attention_mask=padding_mask,
                                                   token_type_ids=batch_seg,
                                                   position_ids=None,
                                                   start_positions=batch_label[:, 0],
                                                   end_positions=batch_label[:, 1])
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            losses += loss.item()
            acc_start = (start_logits.argmax(1) == batch_label[:, 0]).float().mean()
            acc_end = (end_logits.argmax(1) == batch_label[:, 1]).float().mean()
            acc = (acc_start + acc_end) / 2
            if idx % 10 == 0:
                logging.info(f"Epoch: {epoch}, Batch[{idx}/{len(train_iter)}], "
                             f"Train loss :{loss.item():.3f}, Train acc: {acc:.3f}")
            if idx % 100 == 0:
                y_pred = [start_logits.argmax(1), end_logits.argmax(1)]
                y_true = [batch_label[:, 0], batch_label[:, 1]]
                show_result(batch_input, data_loader.vocab.itos,
                            y_pred=y_pred, y_true=y_true)
        end_time = time.time()
        train_loss = losses / len(train_iter)
        logging.info(f"Epoch: {epoch}, Train loss: "
                     f"{train_loss:.3f}, Epoch time = {(end_time - start_time):.3f}s")
        if (epoch + 1) % config.model_val_per_epoch == 0:
            acc, y_pred = evaluate(val_iter, model,
                                   config.device,
                                   data_loader.PAD_IDX,
                                   inference=False)
            batch_input = next(iter(val_iter))[0]
            y_true = next(iter(val_iter))[2]
            show_result(batch_input, data_loader.vocab.itos, y_pred=y_pred, y_true=y_true)

            if acc > max_acc:
                max_acc = acc
                torch.save(model.state_dict(), model_save_path)


def evaluate(data_iter, model, device, PAD_IDX, inference=False):
    model.eval()
    with torch.no_grad():
        acc_sum, n = 0.0, 0
        y_start_pred, y_end_pred = [], []
        for batch_input, batch_seg, batch_label in data_iter:
            batch_input = batch_input.to(device)  # [src_len, batch_size]
            batch_seg = batch_seg.to(device)
            batch_label = batch_label.to(device)
            padding_mask = (batch_input == PAD_IDX).transpose(0, 1)
            start_logits, end_logits = model(input_ids=batch_input,
                                             attention_mask=padding_mask,
                                             token_type_ids=batch_seg,
                                             position_ids=None)

            y_start_pred.append(start_logits.argmax(1).cpu().numpy())
            y_end_pred.append(end_logits.argmax(1).cpu().numpy())
            if not inference:
                acc_sum_start = (start_logits.argmax(1) == batch_label[:, 0]).float().sum().item()
                acc_sum_end = (end_logits.argmax(1) == batch_label[:, 1]).float().sum().item()
                acc_sum += (acc_sum_start + acc_sum_end)
                n += len(batch_label)
        model.train()
        if inference:
            return [np.hstack(y_start_pred), np.hstack(y_end_pred)]
        return acc_sum / (2 * n), [np.hstack(y_start_pred), np.hstack(y_end_pred)]


def show_result(batch_input, itos, num_show=5, y_pred=None, y_true=None):
    """
    本函数的作用是在训练模型的过程中展示相应的结果
    :param batch_input:
    :param itos:
    :param num_show:
    :param y_pred:
    :param y_true:
    :return:
    """
    count = 0
    batch_input = batch_input.transpose(0, 1)  # 转换为[batch_size, seq_len]形状
    for i in range(len(batch_input)):  # 取一个batch所有的原始文本
        if count == num_show:
            break
        input_tokens = [itos[s] for s in batch_input[i]]  # 将question+context 的ids序列转为字符串
        start_pos, end_pos = y_pred[0][i], y_pred[1][i]
        answer_text = " ".join(input_tokens[start_pos:(end_pos + 1)]).replace(" ##", "")
        input_text = " ".join(input_tokens).replace(" ##", "").split('[SEP]')
        question_text, context_text = input_text[0], input_text[1]

        logging.info(f"[{count + 1}/{num_show}] ### context:  {context_text}")
        logging.info(f" ### Question: {question_text}")
        logging.info(f" ### Predicted answer: {answer_text}")
        start_pos, end_pos = y_true[0][i], y_true[1][i]
        true_answer_text = " ".join(input_tokens[start_pos:(end_pos + 1)])
        true_answer_text = true_answer_text.replace(" ##", "")
        logging.info(f" ### True answer: {true_answer_text}")
        logging.info(f" ### True answer idx: {start_pos, end_pos}")
        count += 1


if __name__ == '__main__':
    model_config = ModelConfig()
    train(config=model_config)