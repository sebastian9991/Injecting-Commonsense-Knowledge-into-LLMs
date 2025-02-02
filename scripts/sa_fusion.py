# import dependencies
import argparse
import evaluate
import numpy as np
from datasets import load_dataset
from transformers import AutoTokenizer, AutoConfig, TrainingArguments
from adapters import AutoAdapterModel, AdapterConfig, Fuse, AdapterTrainer
import os
import sys
import json

# useful functions
def parse_arguments():
    parser = argparse.ArgumentParser(description="Fine-tune a model for a sentiment analysis task.")
    parser.add_argument("--output_dir", type=str, default="./training_output", help="Output directory for training results")
    parser.add_argument("--adapter_cn_dir", type=str, default="", help="Directory containing the pre-trained adapter checkpoint")
    parser.add_argument("--adapter_wiki_dir", type=str, default="", help="Directory containing the pre-trained adapter checkpoint")
    parser.add_argument("--model_name", type=str, default="bert-base-multilingual-cased", help="Name of the pre-trained model")
    parser.add_argument("--lang_adapter_cn", type=str, default="models/fusion/langadapter_cn/mlm/adapter_config.json", help="Name of the language adapter trained with CN")
    parser.add_argument("--lang_adapter_wiki", type=str, default="models/fusion/langadapter_wiki/mlm/adapter_config.json", help="Name of the language adapter trained with Wikipedia")
    parser.add_argument("--learning_rate", type=float, default=1e-4, help="Learning rate for training")
    parser.add_argument("--num_train_epochs", type=int, default=50, help="Number of training epochs")
    parser.add_argument("--per_device_train_batch_size", type=int, default=32, help="Batch size per device during training")
    parser.add_argument("--per_device_eval_batch_size", type=int, default=32, help="Batch size per device during evaluation")
    parser.add_argument("--evaluation_strategy", type=str, default="epoch", help="Evaluation strategy during training")
    parser.add_argument("--save_strategy", type=str, default="epoch", help="Saving strategy during training")
    parser.add_argument("--weight_decay", type=float, default=0.01, help="Weight decay for optimization")
    parser.add_argument("--language", type=str, default='', help="Language at hand")
    return parser.parse_args()

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

def encode_batch(examples, tokenizer):
    """Encodes a batch of input data using the model tokenizer."""
    all_encoded = {"input_ids": [], "attention_mask": [], "labels": []}
    
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

def preprocess_dataset(dataset, tokenizer):
    dataset = dataset.map(lambda sample: encode_batch(sample, tokenizer), batched=True)
    dataset.set_format(columns=["input_ids", "attention_mask", "labels"])
    return dataset


def main():
    args = parse_arguments()

    # prepare model
    config = AutoConfig.from_pretrained(args.model_name)
    tokenizer = AutoTokenizer.from_pretrained(args.model_name)
    model = AutoAdapterModel.from_pretrained(args.model_name, config=config)

    # prepare data
    dataset = load_dataset(f"dgurgurov/{args.language}_sa")
    train_dataset = preprocess_dataset(dataset["train"], tokenizer)
    val_dataset = preprocess_dataset(dataset["validation"], tokenizer)
    test_dataset = preprocess_dataset(dataset["test"], tokenizer)

    # load CN language adapter
    lang_adapter_cn = AdapterConfig.load(args.lang_adapter_cn)
    model.load_adapter(args.adapter_cn_dir, config=lang_adapter_cn, load_as="cn", with_head=False)

    # load WIKI language adapter
    lang_adapter_wiki = AdapterConfig.load(args.lang_adapter_wiki)
    model.load_adapter(args.adapter_wiki_dir, config=lang_adapter_wiki, load_as="wiki", with_head=False)

    # add task adapter
    model.add_adapter("cl")
    model.add_classification_head("cl", num_labels=2)
    model.config.prediction_heads['cl']['dropout_prob'] = 0.5
    model.train_adapter(["cl"])

    # set up fusion
    model.add_adapter_fusion(Fuse('cn', 'wiki'))
    model.set_active_adapters(Fuse('cn', 'wiki'))

    adapter_setup = Fuse('cn', 'wiki')
    model.train_adapter_fusion(adapter_setup)

    print(model.adapter_summary())

    training_args = TrainingArguments(
        learning_rate=args.learning_rate,
        num_train_epochs=args.num_train_epochs,
        per_device_train_batch_size=args.per_device_train_batch_size,
        per_device_eval_batch_size=args.per_device_eval_batch_size,
        save_strategy=args.save_strategy,
        evaluation_strategy=args.evaluation_strategy,
        weight_decay=args.weight_decay,
        load_best_model_at_end=True,
        output_dir=args.output_dir,
        overwrite_output_dir=True,
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

    # train model
    trainer.train()

    # test model
    test_score = calculate_f1_on_test_set(trainer, test_dataset, tokenizer)
    print(test_score)
    output_file_path = os.path.join(args.output_dir, "test_metrics.json")
    with open(output_file_path, "w") as json_file:
        json.dump(test_score, json_file, indent=2)


if __name__ == "__main__":
    main()
