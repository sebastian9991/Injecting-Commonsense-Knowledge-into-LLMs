# import dependencies
import argparse
import evaluate
import numpy as np
import os
import re
import sys
import json
import torch
import transformers

from sklearn.model_selection import train_test_split
from datasets import load_dataset
from transformers import (
    DataCollatorForLanguageModeling,
    AutoModelForCausalLM,
    AutoModelForMaskedLM,
    AutoTokenizer,
    BertForSequenceClassification,
    AutoConfig,
    BertConfig,
    Trainer,
    TrainingArguments,
)
from adapters import AutoAdapterModel
from adapters import AdapterConfig
from adapters import AdapterTrainer

# useful functions
def parse_arguments():
    parser = argparse.ArgumentParser(description="Fine-tune a model for a sentiment analysis task.")
    parser.add_argument("--language", type=str, default="", help="Language at hand")
    parser.add_argument("--output_dir", type=str, default="./training_output", help="Output directory for training results")
    parser.add_argument("--adapter_dir", type=str, default="", help="Directory containing the pre-trained adapter checkpoint")
    parser.add_argument("--model_name", type=str, default="bert-base-multilingual-cased", help="Name of the pre-trained model")
    parser.add_argument("--learning_rate", type=float, default=1e-4, help="Learning rate for training")
    parser.add_argument("--num_train_epochs", type=int, default=50, help="Number of training epochs")
    parser.add_argument("--per_device_train_batch_size", type=int, default=32, help="Batch size per device during training")
    parser.add_argument("--per_device_eval_batch_size", type=int, default=32, help="Batch size per device during evaluation")
    parser.add_argument("--evaluation_strategy", type=str, default="epoch", help="Evaluation strategy during training")
    parser.add_argument("--save_strategy", type=str, default="no", help="Saving strategy during training")
    parser.add_argument("--weight_decay", type=float, default=0.01, help="Weight decay for optimization")
    return parser.parse_args()

def encode_batch(examples):
    """Encodes a batch of input data using the model tokenizer."""
    all_encoded = {"input_ids": [], "attention_mask": [], "labels": []}
    tokenizer = AutoTokenizer.from_pretrained('bert-base-multilingual-cased')

    for text, label in zip(examples["text"], examples["label"]):
        encoded = tokenizer(
            text,
            max_length=512,
            truncation=True,
            padding="max_length",
        )
        all_encoded["input_ids"].append(encoded["input_ids"])
        all_encoded["attention_mask"].append(encoded["attention_mask"])
        all_encoded["labels"].append(label)
    
    return all_encoded

def preprocess_dataset(dataset):
    dataset = dataset.map(encode_batch, batched=True)
    dataset.set_format(columns=["input_ids", "attention_mask", "labels"])
    return dataset

def calculate_f1_on_test_set(trainer, test_dataset, tokenizer):
    print("Calculating F1 score on the test set...")
    test_predictions = trainer.predict(test_dataset)

    f1_metric = evaluate.load("f1")
    test_metrics = {
        "f1": f1_metric.compute(
            predictions=np.argmax(test_predictions.predictions, axis=-1),
            references=test_predictions.label_ids,
            average="macro",
        )["f1"],
    }

    print("Test F1 score:", test_metrics["f1"])
    return test_metrics


def main():
    args = parse_arguments()

    # prepare data
    dataset = load_dataset(f"dgurgurov/{args.language}_sa")

    train_dataset = dataset["train"]
    val_dataset = dataset["validation"]
    test_dataset = dataset["test"]
    
    tokenizer = AutoTokenizer.from_pretrained(args.model_name)

    train_dataset = preprocess_dataset(train_dataset)
    val_dataset = preprocess_dataset(val_dataset)
    test_dataset = preprocess_dataset(test_dataset)

    # prepare model
    config = AutoConfig.from_pretrained(args.model_name)
    model = AutoAdapterModel.from_pretrained(args.model_name, config=config)

    # add task adapter
    model.add_adapter("sa")

    # set up task adapter
    model.add_classification_head("sa", num_labels=2)
    model.config.prediction_heads['sa']['dropout_prob'] = 0.5
    model.train_adapter(["sa"])
    
    print(model.adapter_summary())

    training_args = TrainingArguments(
        learning_rate=args.learning_rate,
        num_train_epochs=args.num_train_epochs,
        per_device_train_batch_size=args.per_device_train_batch_size,
        per_device_eval_batch_size=args.per_device_eval_batch_size,
        save_strategy=args.save_strategy,
        evaluation_strategy=args.evaluation_strategy,
        weight_decay=args.weight_decay,
        output_dir=args.output_dir,
        overwrite_output_dir=True,
        load_best_model_at_end=True,
        save_total_limit=1,
    )

    f1_metric = evaluate.load("f1")
    
    trainer = AdapterTrainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=val_dataset,
        compute_metrics=lambda pred: {
                "f1": f1_metric.compute(
                    predictions=np.argmax(pred.predictions, axis=-1),
                    references=pred.label_ids,
                    average="macro",
                )["f1"],
            },
    )

    trainer.train()

    calculate_f1_on_test_set(trainer, test_dataset, tokenizer)

    output_file_path = os.path.join(args.output_dir, "test_metrics.json")
    with open(output_file_path, "w") as json_file:
        json.dump(calculate_f1_on_test_set(trainer, test_dataset, tokenizer), json_file, indent=2)


if __name__ == "__main__":
    main()
