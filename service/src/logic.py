import os
import re

from tqdm import tqdm
import logging

import torch
from transformers import TextDataset, DataCollatorForLanguageModeling
from torch.utils.data import DataLoader

from accelerate import Accelerator
from transformers import AdamW, AutoModelForSequenceClassification, get_scheduler
from transformers import AutoModelForCausalLM, AutoTokenizer

logging.basicConfig(format="%(asctime)s %(message)s",
                    datefmt="%I:%M:%S %p", level=logging.INFO)
class NeuralNetwork:
    def __init__(self, group_id=0):
        self.DEVICE = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        checkpoint = "Kirili4ik/ruDialoGpt3-medium-finetuned-telegram"
        self.tokenizer = AutoTokenizer.from_pretrained(checkpoint)
        self.model = AutoModelForCausalLM.from_pretrained(checkpoint)
        self.group_id = group_id
        self.train_dataset = None
        self.test_dataset = None
        self.data_collator = None

    def get_length_param(self, text: str) -> str:
        tokens_count = len(self.tokenizer.encode(text))
        if tokens_count <= 15:
            len_param = '1'
        elif tokens_count <= 50:
            len_param = '2'
        elif tokens_count <= 256:
            len_param = '3'
        else:
            len_param = '-'
        return len_param

    def build_text_file(self, texts: list[str], dest_path: str = "train_test_datasest/"):
        with open(dest_path, 'w') as f:
            for text in texts:
                post_text = re.sub(r"\n", ". ", text)
                if len(post_text) == 0 or type(post_text) != str:
                    continue
                length = self.get_length_param(post_text)
                f.write(f"|{length}|{post_text}{self.tokenizer.eos_token}\n")

    def load_dataset(self, train_path, test_path):
        self.train_dataset = TextDataset(
            tokenizer=self.tokenizer,
            file_path=train_path,
            block_size=256
        )

        self.test_dataset = TextDataset(
            tokenizer=self.tokenizer,
            file_path=test_path,
            block_size=256
        )

        self.data_collator = DataCollatorForLanguageModeling(
            tokenizer=self.tokenizer, mlm=False
        )

    def tune(self, texts, checkpoint_path="weights/", train_dataset_path="train_test_datasets/train", test_dataset_path="train_test_datasets/test"):
        train_dataset_path = train_dataset_path + str(self.group_id)
        test_dataset_path = test_dataset_path + str(self.group_id)
        self.build_text_file(texts[int(len(texts)*0.1):], dest_path=train_dataset_path)
        self.build_text_file(texts[:int(len(texts)*0.1)], dest_path=test_dataset_path)
        self.load_dataset(train_dataset_path, test_dataset_path)

        train_loader = DataLoader(self.train_dataset, shuffle=True, batch_size=1, collate_fn=self.data_collator)
        test_loader = DataLoader(self.test_dataset, batch_size=1, collate_fn=self.data_collator)

        num_epochs = 3
        optimizer = AdamW(self.model.parameters(), lr=3e-5)
        save_checkpoint_path = checkpoint_path + str(self.group_id) + ".pt"

        num_training_steps = num_epochs * len(self.train_dataset)
        lr_scheduler = get_scheduler(
            "linear",
            optimizer=optimizer,
            num_warmup_steps=30,
            num_training_steps=num_training_steps
        )

        accelerator = Accelerator()
        train_dl, test_dl, self.model, optimizer = accelerator.prepare(
            train_loader, test_loader, self.model, optimizer
        )
        logging.info(f"len of all texts: {len(texts)}")
        progress_bar = tqdm(range(num_training_steps))
        logging.info("start tuning")
        f = open(f"logs/logs-{self.group_id}.txt", "w")
        try:
            for epoch in range(num_epochs):
                self.model.train()
                for batch in train_dl:
                    optimizer.zero_grad()
                    outputs = self.model(**batch)
                    loss = outputs.loss
                    accelerator.backward(loss)
                    optimizer.step()
                    lr_scheduler.step()
                    progress_bar.update(1)

                torch.save({
                    'model_state_dict': self.model.state_dict(),
                }, save_checkpoint_path)

                cum_loss = 0
                self.model.eval()
                with torch.inference_mode():
                    for batch in test_dl:
                        outputs = self.model(**batch)
                        cum_loss += float(outputs.loss.item())
                logging.info(str(cum_loss / len(test_loader)))
                f.write(f"All is ok. {cum_loss / len(test_loader)}")
        except Exception as e:
            logging.error(f"An error occured: {e}")
        finally:
            f.close()
        os.rename(save_checkpoint_path, checkpoint_path + str(self.group_id) + "-trained.pt")

    def load_weights(self, group_id, checkpoint_path="weights/"):
        checkpoint_path = checkpoint_path + str(group_id) + "-trained.pt"
        checkpoint = torch.load(checkpoint_path, map_location=self.DEVICE)
        self.model.load_state_dict(checkpoint['model_state_dict'])
        print("weights loaded")

    def generate(self, hint):
        logging.info("generating")
        text = "<|startoftext|>" + hint
        input_ids = self.tokenizer.encode(text, return_tensors="pt").to(self.DEVICE)
        self.model.eval()
        with torch.no_grad():
            out = self.model.generate(input_ids,
                                      do_sample=True,
                                      temperature=1.5,
                                      top_k=50,
                                      top_p=0.9,
                                      max_length=150,
                                      num_return_sequences=1,
                                      eos_token_id=self.tokenizer.eos_token_id,
                                      pad_token_id=self.tokenizer.pad_token_id,
                                      )
        generated_text = list(map(self.tokenizer.decode, out))[0]
        generated_text = generated_text.replace("<|startoftext|>", "")
        generated_text = generated_text.split("</s>")[0].strip()
        return generated_text
