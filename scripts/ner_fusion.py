# import dependencies
import argparse
import numpy as np
import json
import os
from datasets import load_dataset, load_metric
from transformers import AutoTokenizer, AutoConfig, TrainingArguments, Trainer, DataCollatorForTokenClassification
from adapters import AutoAdapterModel, Fuse
from adapters import AdapterConfig
from adapters import AdapterTrainer
from adapters.composition import Stack

# useful functions
def compute_metrics(p, label_names):
    predictions, labels = p
    predictions = np.argmax(predictions, axis=2)
    true_predictions = [
        [label_names[p] for (p, l) in zip(prediction, label) if l != -100]
        for prediction, label in zip(predictions, labels)
    ]
    true_labels = [
        [label_names[l] for (p, l) in zip(prediction, label) if l != -100]
        for prediction, label in zip(predictions, labels)
    ]
    metric = load_metric("seqeval")
    results = metric.compute(predictions=true_predictions, references=true_labels)
    flattened_results = {
        "overall_precision": results["overall_precision"],
        "overall_recall": results["overall_recall"],
        "overall_f1": results["overall_f1"],
        "overall_accuracy": results["overall_accuracy"],
    }
    for k in results.keys():
        if k not in flattened_results.keys():
            flattened_results[k+"_f1"] = results[k]["f1"]
    return flattened_results

def main():
    parser = argparse.ArgumentParser(description="Fine-tune a model for a sentiment analysis task.")
    parser.add_argument("--output_dir", type=str, default="./training_output", help="Output directory for training results")
    parser.add_argument("--logging_dir", type=str, default="./models/logs", help="Output directory for log results")
    parser.add_argument("--model_name", type=str, default="bert-base-multilingual-cased", help="Name of the pre-trained model")
    parser.add_argument("--adapter_cn_dir", type=str, default="", help="Directory containing the pre-trained adapter checkpoint")
    parser.add_argument("--adapter_cn_config", type=str, default="", help="Directory containing the pre-trained adapter checkpoint")
    parser.add_argument("--adapter_wiki_dir", type=str, default="", help="Directory containing the pre-trained adapter checkpoint")
    parser.add_argument("--adapter_wiki_config", type=str, default="", help="Directory containing the pre-trained adapter checkpoint")
    parser.add_argument("--learning_rate", type=float, default=1e-4, help="Learning rate for training")
    parser.add_argument("--num_train_epochs", type=int, default=30, help="Number of training epochs")
    parser.add_argument("--per_device_train_batch_size", type=int, default=64, help="Batch size per device during training")
    parser.add_argument("--per_device_eval_batch_size", type=int, default=64, help="Batch size per device during evaluation")
    parser.add_argument("--evaluation_strategy", type=str, default="epoch", help="Evaluation strategy during training")
    parser.add_argument("--save_strategy", type=str, default="epoch", help="Saving strategy during training")
    parser.add_argument("--weight_decay", type=float, default=0.01, help="Weight decay for optimization")
    parser.add_argument("--language", type=str, help='Language for fine-tuning')
    args = parser.parse_args()

    def tokenize_adjust_labels(all_samples_per_split):
        tokenized_samples = tokenizer.batch_encode_plus(all_samples_per_split["tokens"], is_split_into_words=True)
        # tokenized_samples is not a datasets object so this alone won't work with Trainer API, hence map is used 
        # so the new keys [input_ids, labels (after adjustment)]
        # can be added to the datasets dict for each train test validation split
        total_adjusted_labels = []
        for k in range(0, len(tokenized_samples["input_ids"])):
            prev_wid = -1
            word_ids_list = tokenized_samples.word_ids(batch_index=k)
            existing_label_ids = all_samples_per_split["ner_tags"][k]
            i = -1
            adjusted_label_ids = []
        
            for wid in word_ids_list:
                if(wid is None):
                    adjusted_label_ids.append(-100)
                elif(wid!=prev_wid):
                    i = i + 1
                    adjusted_label_ids.append(existing_label_ids[i])
                    prev_wid = wid
                else:
                    label_name = label_names[existing_label_ids[i]]
                    adjusted_label_ids.append(existing_label_ids[i])
                
            total_adjusted_labels.append(adjusted_label_ids)
        tokenized_samples["labels"] = total_adjusted_labels
        tokenized_samples["labels"] = [list(map(int, x)) for x in tokenized_samples["labels"]]

        return tokenized_samples

    # prepare data
    dataset = load_dataset("wikiann", str(args.language))
    label_names = dataset["train"].features["ner_tags"].feature.names

    tokenizer = AutoTokenizer.from_pretrained(args.model_name)
    tokenized_dataset = dataset.map(tokenize_adjust_labels, batched=True)

    # prepare model
    config = AutoConfig.from_pretrained(args.model_name)
    model = AutoAdapterModel.from_pretrained(args.model_name, config=config)
    
    # load language adapters
    lang_adapter_cn = AdapterConfig.load(args.adapter_cn_config)
    model.load_adapter(args.adapter_cn_dir, config=lang_adapter_cn, load_as="cn", with_head=False)

    lang_adapter_wiki = AdapterConfig.load(args.adapter_wiki_config)
    model.load_adapter(args.adapter_wiki_dir, config=lang_adapter_wiki, load_as="wiki", with_head=False)

    # add task adapter
    model.add_adapter("ner")
    model.add_tagging_head("ner", num_labels=len(label_names))
    model.config.prediction_heads['ner']['dropout_prob'] = 0.2
    model.train_adapter("ner")

    # set up adapter fusion
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
        output_dir=args.output_dir,
        overwrite_output_dir=True,
        save_total_limit=1,
        load_best_model_at_end=True,
        save_only_model=True,
    )

    trainer = AdapterTrainer(
        model=model,
        args=training_args,
        train_dataset=tokenized_dataset["train"],
        eval_dataset=tokenized_dataset["validation"],
        data_collator=DataCollatorForTokenClassification(tokenizer),
        tokenizer=tokenizer,
        compute_metrics=lambda p: compute_metrics(p, label_names)
    )

    # train model
    trainer.train()

    # test model
    test_results = trainer.evaluate(tokenized_dataset["test"])
    output_file_path = os.path.join(args.output_dir, "test_metrics.json")
    with open(output_file_path, "w") as f:
        json.dump(test_results, f)

if __name__ == "__main__":
    main()
