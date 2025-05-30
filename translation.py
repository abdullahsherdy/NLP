import os
import warnings
from datasets import load_dataset
from sklearn.model_selection import train_test_split
import pandas as pd
import re
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
from transformers import MarianTokenizer, MarianMTModel, Seq2SeqTrainingArguments, Seq2SeqTrainer
from datasets import Dataset, DatasetDict
import nltk
from nltk.corpus import stopwords
from nltk.stem.isri import ISRIStemmer
import emoji
from datetime import datetime
from transformers import DataCollatorForSeq2Seq
import torch
from wordcloud import WordCloud
from collections import defaultdict
import arabic_reshaper
from bidi.algorithm import get_display
import unicodedata
from nltk.translate.bleu_score import sentence_bleu, SmoothingFunction
from rouge_score import rouge_scorer
from transformers import EarlyStoppingCallback

# General settings
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
os.environ['TF_ENABLE_ONEDNN_OPTS'] = '0'
warnings.filterwarnings('ignore')

# Training control
FORCE_RETRAIN = False # Changed to True to force retraining

# Check GPU availability
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# Model configurations
MODEL_CONFIGS = {
    'ar-en': {
        'model_name': 'Helsinki-NLP/opus-mt-ar-en',
        'src_lang': 'ar',
        'tgt_lang': 'en',
        'src_stopwords': set(),
        'tgt_stopwords': set(),
        'preserve_words': {
            'الى', 'على', 'في', 'عن', 'من', 'يوم', 'سنة', 'شهر',
            'كان', 'يكون', 'يكونون', 'كنت', 'كانت', 'سوف', 'قد',
            'لن', 'لم', 'لا', 'ما', 'ماذا', 'من', 'الى', 'عند',
            'بعد', 'قبل', 'حيث', 'التي', 'الذي', 'الذين'
        }
    },
    'en-ar': {
        'model_name': 'Helsinki-NLP/opus-mt-en-ar',
        'src_lang': 'en',
        'tgt_lang': 'ar',
        'src_stopwords': set(),
        'tgt_stopwords': set(),
        'preserve_words': {
            'to', 'on', 'in', 'at', 'from', 'day', 'year', 'month',
            'was', 'is', 'are', 'were', 'will', 'would', 'could',
            'should', 'have', 'has', 'had', 'do', 'does', 'did',
            'not', 'no', 'yes', 'who', 'what', 'when', 'where',
            'why', 'how', 'which', 'that', 'this', 'these', 'those'
        }
    }
}


class BilingualTranslationPipeline:

    def __init__(self, direction='ar-en'):
        self.direction = direction
        self.config = MODEL_CONFIGS[direction]
        self.tokenizer = None
        self.model = None
        self.dataset = None
        self.log_file = f"translation_log_{direction}.txt"
        self.device = device
        self.translation_cache = defaultdict(dict)

        # Initialize Arabic text normalizer
        self.arabic_normalizer = {
            'إ': 'ا', 'أ': 'ا', 'آ': 'ا', 'ٱ': 'ا',
            'ى': 'ي', 'ة': 'ه'
        }

        with open(self.log_file, 'w', encoding='utf-8') as f:
            f.write(f"Translation Pipeline Log - {direction}\n")
            f.write(f"Using device: {self.device}\n")
            f.write("=" * 50 + "\n")

    def log_step(self, message, status="info"):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        status_map = {
            "start": "🚀 START",
            "success": "✅ SUCCESS",
            "error": "❌ ERROR",
            "info": "ℹ️ INFO",
            "warning": "⚠️ WARNING"
        }
        log_entry = f"[{timestamp}] [{status_map.get(status, 'INFO')}] {message}"
        print(log_entry)

        with open(self.log_file, 'a', encoding='utf-8') as f:
            f.write(log_entry + "\n")

        if status == "start":
            print("-" * 80)

    def setup_resources(self):
        self.log_step("Initializing NLTK resources", "start")
        try:
            nltk.download('punkt', quiet=True)
            nltk.download('stopwords', quiet=True)
            self.log_step("NLTK resources ready", "success")
        except Exception as e:
            self.log_step(f"Failed to initialize NLTK: {e}", "error")
            exit()

    def normalize_arabic(self, text):
        text = unicodedata.normalize('NFKD', text)
        text = ''.join([self.arabic_normalizer.get(c, c) for c in text])
        text = re.sub(r'[\u064B-\u065F]', '', text)
        return text

    def load_data(self):
        self.log_step(f"Loading Tatoeba dataset for {self.direction} translation", "start")
        try:
            # Load Tatoeba dataset from HuggingFace
            dataset = load_dataset("tatoeba", lang1="ar", lang2="en")

            # Convert structure if needed
            if 'translation' in dataset['train'].features:
                dataset = dataset.map(lambda x: {
                    'ar': x['translation']['ar'],
                    'en': x['translation']['en']
                })

            # For en-ar direction, swap the columns
            if self.direction == 'en-ar':
                dataset = dataset.map(lambda x: {'src': x['en'], 'tgt': x['ar']})
            else:
                dataset = dataset.map(lambda x: {'src': x['ar'], 'tgt': x['en']})

            # Convert to DatasetDict to be compatible with the rest of the code
            self.dataset = DatasetDict({'train': dataset['train']})

            stats = {
                'total_samples': len(self.dataset['train']),
                'avg_src_len': np.mean([len(str(x['src']).split()) for x in self.dataset['train']]),
                'avg_tgt_len': np.mean([len(str(x['tgt']).split()) for x in self.dataset['train']]),
                'sample_src': str(self.dataset['train'][0]['src'])[:100],
                'sample_tgt': str(self.dataset['train'][0]['tgt'])[:100]
            }

            self.log_step(f"Loaded {stats['total_samples']:,} samples from Tatoeba", "success")
            self.log_step(f"Average src length: {stats['avg_src_len']:.1f} words", "info")
            self.log_step(f"Average tgt length: {stats['avg_tgt_len']:.1f} words", "info")
            self.log_step(f"Sample src: {stats['sample_src']}...", "info")
            self.log_step(f"Sample tgt: {stats['sample_tgt']}...", "info")

            return stats
        except Exception as e:
            self.log_step(f"Error loading Tatoeba dataset: {str(e)}", "error")
            exit()

    def clean_text(self, text, lang):
        if not isinstance(text, str):
            text = str(text)

        text = re.sub(r'\s+', ' ', text).strip()
        text = emoji.replace_emoji(text, replace='')

        if lang == 'ar':
            text = self.normalize_arabic(text)
            text = re.sub(r'[^\w\s\u0600-\u06FF.,!?؛،:\'"()\-]', ' ', text)
        else:
            text = re.sub(r'[^\w\s.,!?\'"()\-]', ' ', text)

        return text if text else None

    def clean_data(self):
        self.log_step("Cleaning and preprocessing dataset", "start")
        try:
            original_count = len(self.dataset['train'])

            self.dataset = self.dataset.map(lambda x: {
                'src': self.clean_text(x['src'], self.config['src_lang']),
                'tgt': self.clean_text(x['tgt'], self.config['tgt_lang'])
            }, batched=False).filter(
                lambda x: x['src'] is not None and x['tgt'] is not None)

            cleaned_count = len(self.dataset['train'])
            removed_count = original_count - cleaned_count

            self.visualize_wordclouds()

            self.log_step(
                f"Cleaning complete. Removed {removed_count:,} samples ({removed_count / original_count:.1%})",
                "success")
            self.log_step(f"Remaining samples: {cleaned_count:,}", "info")

            return {
                'original_count': original_count,
                'cleaned_count': cleaned_count,
                'removed_count': removed_count
            }
        except Exception as e:
            self.log_step(f"Error during cleaning: {str(e)}", "error")
            exit()

    def visualize_wordclouds(self):
        try:
            os.makedirs('visualizations', exist_ok=True)

            src_text = ' '.join([x['src'] for x in self.dataset['train']])
            tgt_text = ' '.join([x['tgt'] for x in self.dataset['train']])

            if self.config['src_lang'] == 'ar':
                src_text = get_display(arabic_reshaper.reshape(src_text))
            if self.config['tgt_lang'] == 'ar':
                tgt_text = get_display(arabic_reshaper.reshape(tgt_text))

            plt.figure(figsize=(16, 8))

            plt.subplot(1, 2, 1)
            src_wc = WordCloud(
                width=800, height=400,
                background_color='white',
                font_path='tahoma' if self.config['src_lang'] == 'ar' else 'arial',
                collocations=False
            ).generate(src_text)
            plt.imshow(src_wc, interpolation='bilinear')
            plt.axis('off')
            plt.title(f'{self.config["src_lang"].upper()} Word Cloud')

            plt.subplot(1, 2, 2)
            tgt_wc = WordCloud(
                width=800, height=400,
                background_color='white',
                font_path='tahoma' if self.config['tgt_lang'] == 'ar' else 'arial',
                collocations=False
            ).generate(tgt_text)
            plt.imshow(tgt_wc, interpolation='bilinear')
            plt.axis('off')
            plt.title(f'{self.config["tgt_lang"].upper()} Word Cloud')

            plt.tight_layout()
            plt.savefig(f'visualizations/wordclouds_{self.direction}.png', bbox_inches='tight', dpi=300)
            plt.close()

            self.log_step("Word clouds generated successfully", "info")
        except Exception as e:
            self.log_step(f"Could not generate word clouds: {str(e)}", "info")

    def filter_data(self):
        self.log_step("Filtering by sentence length", "start")
        try:
            def length_filter(example):
                src_len = len(example['src'].split())
                tgt_len = len(example['tgt'].split())
                return 1 <= src_len <= 128 and 1 <= tgt_len <= 128

            before_count = len(self.dataset['train'])
            self.dataset = self.dataset.filter(length_filter)
            after_count = len(self.dataset['train'])
            removed_count = before_count - after_count

            self.visualize_length_distributions()

            self.log_step(f"Filtered from {before_count:,} to {after_count:,} samples", "success")
            self.log_step(f"Removed {removed_count:,} samples ({removed_count / before_count:.1%})", "info")

            return {
                'before_count': before_count,
                'after_count': after_count,
                'removed_count': removed_count
            }
        except Exception as e:
            self.log_step(f"Error during filtering: {str(e)}", "error")
            exit()

    def visualize_length_distributions(self):
        try:
            src_lengths = [len(x['src'].split()) for x in self.dataset['train']]
            tgt_lengths = [len(x['tgt'].split()) for x in self.dataset['train']]

            plt.figure(figsize=(16, 6))

            plt.subplot(1, 2, 1)
            sns.histplot(src_lengths, bins=30, kde=True)
            plt.title(f'{self.config["src_lang"].upper()} Sentence Length Distribution')
            plt.xlabel('Length in words')
            plt.ylabel('Count')

            plt.subplot(1, 2, 2)
            sns.histplot(tgt_lengths, bins=30, kde=True)
            plt.title(f'{self.config["tgt_lang"].upper()} Sentence Length Distribution')
            plt.xlabel('Length in words')
            plt.ylabel('Count')

            plt.tight_layout()
            plt.savefig(f'visualizations/length_distributions_{self.direction}.png', bbox_inches='tight', dpi=300)
            plt.close()

            self.log_step("Length distributions visualized", "info")
        except Exception as e:
            self.log_step(f"Could not generate length distributions: {str(e)}", "info")

    def split_data(self):
        self.log_step("Splitting dataset", "start")
        try:
            df = pd.DataFrame(self.dataset['train'])

            train_df, temp_df = train_test_split(
                df,
                test_size=0.2,
                random_state=42
            )
            val_df, _ = train_test_split(
                temp_df,
                test_size=0.5,
                random_state=42
            )

            self.dataset = DatasetDict({
                'train': Dataset.from_pandas(train_df.reset_index(drop=True)),
                'validation': Dataset.from_pandas(val_df.reset_index(drop=True))
            })

            self.visualize_data_split()

            split_counts = {
                'train': len(self.dataset['train']),
                'validation': len(self.dataset['validation'])
            }

            self.log_step("Dataset split completed", "success")
            self.log_step(f"Training samples: {split_counts['train']:,}", "info")
            self.log_step(f"Validation samples: {split_counts['validation']:,}", "info")

            return split_counts
        except Exception as e:
            self.log_step(f"Error during splitting: {str(e)}", "error")
            exit()

    def visualize_data_split(self):
        try:
            split_counts = {
                'Train': len(self.dataset['train']),
                'Validation': len(self.dataset['validation'])
            }

            plt.figure(figsize=(10, 6))

            plt.subplot(1, 2, 1)
            plt.pie(split_counts.values(), labels=split_counts.keys(), autopct='%1.1f%%', startangle=90)
            plt.title('Dataset Split Distribution')

            plt.subplot(1, 2, 2)
            sns.barplot(x=list(split_counts.keys()), y=list(split_counts.values()))
            plt.title('Dataset Split Counts')
            plt.ylabel('Number of Samples')

            plt.tight_layout()
            plt.savefig(f'visualizations/data_split_{self.direction}.png', bbox_inches='tight', dpi=300)
            plt.close()

            self.log_step("Data split visualized", "info")
        except Exception as e:
            self.log_step(f"Could not generate split visualization: {str(e)}", "info")

    def load_model(self):
        self.log_step(f"Loading {self.direction} model", "start")
        try:
            model_path = f"./fine_tuned_model_{self.direction}"

            if os.path.exists(model_path) and not FORCE_RETRAIN:
                self.log_step(f"Loading fine-tuned model from {model_path}", "info")
                self.tokenizer = MarianTokenizer.from_pretrained(model_path)
                self.model = MarianMTModel.from_pretrained(model_path).to(self.device)
            else:
                self.log_step("Loading base model", "info")
                self.tokenizer = MarianTokenizer.from_pretrained(self.config['model_name'])
                self.model = MarianMTModel.from_pretrained(self.config['model_name']).to(self.device)

            test_text = "This is a translation test" if self.direction == 'en-ar' else "هذا اختبار للترجمة"
            try:
                translation = self.translate_text(test_text)
                self.log_step(f"Test translation - Original: {test_text}", "info")
                self.log_step(f"Test translation - Result: {translation['translated']}", "info")
            except Exception as e:
                self.log_step(f"Initial translation test failed: {str(e)}", "warning")

            self.log_step(f"Model loaded successfully", "success")
        except Exception as e:
            self.log_step(f"Error loading model: {str(e)}", "error")
            exit()

    def tokenize_data(self):
        self.log_step("Tokenizing dataset", "start")
        try:
            max_length = 128

            def preprocess_function(examples):
                inputs = [str(x) for x in examples['src']]
                targets = [str(x) for x in examples['tgt']]

                model_inputs = self.tokenizer(
                    inputs,
                    max_length=max_length,
                    truncation=True,
                    padding='max_length',
                    return_tensors="pt"
                ).to(self.device)

                with self.tokenizer.as_target_tokenizer():
                    labels = self.tokenizer(
                        targets,
                        max_length=max_length,
                        truncation=True,
                        padding='max_length',
                        return_tensors="pt"
                    ).to(self.device)

                model_inputs["labels"] = labels["input_ids"]
                return model_inputs

            self.tokenized_datasets = self.dataset.map(
                preprocess_function,
                batched=True,
                remove_columns=['src', 'tgt']
            )

            self.log_step("Tokenization completed", "success")
        except Exception as e:
            self.log_step(f"Error tokenizing data: {str(e)}", "error")
            exit()

    def compute_metrics(self, eval_pred):
        predictions, labels = eval_pred
        decoded_preds = self.tokenizer.batch_decode(predictions, skip_special_tokens=True)

        # Replace -100 in the labels as we can't decode them
        labels = np.where(labels != -100, labels, self.tokenizer.pad_token_id)
        decoded_labels = self.tokenizer.batch_decode(labels, skip_special_tokens=True)

        bleu_scores = []
        rouge_scores = defaultdict(list)
        rouge_scorer_obj = rouge_scorer.RougeScorer(['rouge1', 'rouge2', 'rougeL'], use_stemmer=True)
        smoothie = SmoothingFunction().method1

        for pred, label in zip(decoded_preds, decoded_labels):
            # Calculate BLEU score
            reference_tokens = [label.split()]
            candidate_tokens = pred.split()
            bleu_score = sentence_bleu(reference_tokens, candidate_tokens, smoothing_function=smoothie)
            bleu_scores.append(bleu_score)

            # Calculate ROUGE scores
            scores = rouge_scorer_obj.score(label, pred)
            for metric in ['rouge1', 'rouge2', 'rougeL']:
                rouge_scores[metric].append(scores[metric].fmeasure)

        return {
            'bleu': np.mean(bleu_scores),
            'rouge1': np.mean(rouge_scores['rouge1']),
            'rouge2': np.mean(rouge_scores['rouge2']),
            'rougeL': np.mean(rouge_scores['rougeL']),
            'gen_len': np.mean([len(pred.split()) for pred in decoded_preds])
        }

    def setup_training(self):
        try:
            self.data_collator = DataCollatorForSeq2Seq(
                self.tokenizer,
                model=self.model,
                padding=True
            )

            # Adjust batch size based on available memory
            batch_size = 32 if torch.cuda.is_available() else 16
            grad_accum = 2 if torch.cuda.is_available() else 8

            self.training_args = Seq2SeqTrainingArguments(
                output_dir=f"./results_{self.direction}",
                overwrite_output_dir=True,
                per_device_train_batch_size=batch_size,
                per_device_eval_batch_size=batch_size,
                gradient_accumulation_steps=grad_accum,
                num_train_epochs=3, 
                learning_rate=3e-5,
                warmup_steps=500,
                weight_decay=0.01,
                save_strategy="steps",
                save_steps=2000,
                logging_steps=500,
                fp16=torch.cuda.is_available(),
                report_to="none",
                optim="adamw_torch",
                predict_with_generate=True,
                generation_max_length=128,
                seed=42,
                label_smoothing_factor=0.1,
                logging_dir=f"./logs_{self.direction}",
                group_by_length=True,
             
            )

            self.trainer = Seq2SeqTrainer(
                model=self.model,
                args=self.training_args,
                train_dataset=self.tokenized_datasets["train"],
                eval_dataset=self.tokenized_datasets["validation"],
                data_collator=self.data_collator,
                tokenizer=self.tokenizer,
                compute_metrics=self.compute_metrics,
        
            )

            self.log_step("Training configuration completed with optimizations", "success")
        except Exception as e:
            self.log_step(f"Error configuring training: {str(e)}", "error")
            exit()

    def train_model(self):
        model_path = f"./fine_tuned_model_{self.direction}"

        # if os.path.exists(model_path) and not FORCE_RETRAIN:
        #     self.log_step("Model already trained. Skipping training.", "info")
        #     return {
        #         'train_loss': 0.0,
        #         'train_samples': len(self.tokenized_datasets["train"])
        #     }

        self.log_step("Starting model training", "start")
        try:
            train_result = self.trainer.train()

           
            self.trainer.save_model(model_path)
            self.tokenizer.save_pretrained(model_path)
            self.model.save_pretrained(model_path, save_function=torch.save)

            metrics = train_result.metrics
            metrics['train_samples'] = len(self.tokenized_datasets["train"])

            with open(f'training_metrics_{self.direction}.txt', 'w') as f:
                for key, value in metrics.items():
                    f.write(f"{key}: {value}\n")

            self.visualize_training_metrics(metrics)

            self.log_step("Training completed", "success")
            return metrics
        except Exception as e:
            self.log_step(f"Error during training: {str(e)}", "error")
            exit()
    def visualize_training_metrics(self, metrics):
        try:
            os.makedirs('visualizations', exist_ok=True)

            plt.figure(figsize=(12, 6))
            plt.plot([metrics['train_loss']], label='Training Loss', marker='o')
            plt.title('Training Loss')
            plt.xlabel('Epoch')
            plt.ylabel('Loss')
            plt.legend()
            plt.grid(True)
            plt.tight_layout()
            plt.savefig(f'visualizations/training_metrics_{self.direction}.png', bbox_inches='tight', dpi=300)
            plt.close()

            self.log_step("Training metrics visualized", "info")
        except Exception as e:
            self.log_step(f"Could not generate training metrics plot: {str(e)}", "info")

    def translate_text(self, text):
        try:
            if text in self.translation_cache:
                return self.translation_cache[text]

            src_lang = self.config['src_lang']
            cleaned_text = self.clean_text(text, src_lang)

            inputs = self.tokenizer(cleaned_text, return_tensors="pt").to(self.device)
            translated = self.model.generate(**inputs, max_length=128)
            translated_text = self.tokenizer.decode(translated[0], skip_special_tokens=True)

            if self.direction == 'ar-en':
                translated_text = translated_text[0].upper() + translated_text[1:]
                translated_text = re.sub(r'\b(a|an|the)\s+(a|an|the)\b', r'\1', translated_text)
                translated_text = re.sub(r'\s([?.!,;](?:\s|$))', r'\1', translated_text)
            else:
                translated_text = translated_text.replace('.', '۔')
                translated_text = translated_text.replace(',', '،')
                translated_text = translated_text.replace('"', '«').replace("'", "»")
                translated_text = re.sub(r'\s([؟،؛](?:\s|$))', r'\1', translated_text)

            result = {
                'original': text,
                'cleaned': cleaned_text,
                'translated': translated_text.strip()
            }

            self.translation_cache[text] = result
            return result
        except Exception as e:
            self.log_step(f"Translation error: {str(e)}", "error")
            raise

    def interactive_translation_test(self):
        print("\n" + "=" * 50)
        print(f"Interactive Translation Test ({self.direction})")
        print("Enter 'exit' to quit")
        print("=" * 50)

        while True:
            text = input("\nEnter text to translate: ")
            if text.lower() == 'exit':
                break
            try:
                start_time = datetime.now()
                result = self.translate_text(text)
                elapsed = (datetime.now() - start_time).total_seconds()

                print("\nTranslation Result:")
                print(f"Original: {result['original']}")
                print(f"Cleaned: {result['cleaned']}")
                print(f"Translated: {result['translated']}")
                print(f"Time: {elapsed:.2f}s")

            except Exception as e:
                print(f"Error: {str(e)}")

    def evaluate_translations(self, num_samples=500):
        self.log_step("Starting translation evaluation", "start")
        try:
            bleu_scores = []
            rouge_scores = defaultdict(list)
            rouge_scorer_obj = rouge_scorer.RougeScorer(['rouge1', 'rouge2', 'rougeL'], use_stemmer=True)
            smoothie = SmoothingFunction().method1

            eval_samples = self.dataset['validation'].select(range(min(num_samples, len(self.dataset['validation']))))

            total_samples = len(eval_samples)
            self.log_step(f"Evaluating {total_samples} samples", "info")

            for idx, sample in enumerate(eval_samples):
                source_text = sample['src']
                reference_text = sample['tgt']

                translation_result = self.translate_text(source_text)
                translated_text = translation_result['translated']

                reference_tokens = [reference_text.split()]
                candidate_tokens = translated_text.split()
                bleu_score = sentence_bleu(reference_tokens, candidate_tokens, smoothing_function=smoothie)
                bleu_scores.append(bleu_score)

                rouge_scores_dict = rouge_scorer_obj.score(reference_text, translated_text)
                for metric, score in rouge_scores_dict.items():
                    rouge_scores[metric].append(score.fmeasure)

                if (idx + 1) % 10 == 0:
                    self.log_step(f"Processed {idx + 1}/{total_samples} samples", "info")

            avg_bleu = np.mean(bleu_scores)
            avg_rouge = {metric: np.mean(scores) for metric, scores in rouge_scores.items()}

            evaluation_report = f"""
            ============== TRANSLATION EVALUATION - {self.direction.upper()} ==============

            Evaluation Metrics (averaged over {total_samples} samples):

            BLEU Score: {avg_bleu:.4f}

            ROUGE Scores:
            - ROUGE-1: {avg_rouge['rouge1']:.4f}
            - ROUGE-2: {avg_rouge['rouge2']:.4f}
            - ROUGE-L: {avg_rouge['rougeL']:.4f}

            =====================================================
            """
            with open(f'evaluation_results_{self.direction}.txt', 'w', encoding='utf-8') as f:
                f.write(evaluation_report)

            self.visualize_evaluation_scores(bleu_scores, rouge_scores)

            self.log_step("Evaluation completed", "success")
            print(evaluation_report)

            return {
                'bleu': avg_bleu,
                'rouge': avg_rouge
            }

        except Exception as e:
            self.log_step(f"Error during evaluation: {str(e)}", "error")
            raise

    def visualize_evaluation_scores(self, bleu_scores, rouge_scores):
        try:
            plt.figure(figsize=(15, 5))

            plt.subplot(1, 2, 1)
            sns.histplot(bleu_scores, bins=20, kde=True)
            plt.title('BLEU Score Distribution')
            plt.xlabel('BLEU Score')
            plt.ylabel('Count')

            plt.subplot(1, 2, 2)
            rouge_metrics = list(rouge_scores.keys())
            rouge_values = [np.mean(scores) for scores in rouge_scores.values()]
            sns.barplot(x=rouge_metrics, y=rouge_values)
            plt.title('Average ROUGE Scores')
            plt.xlabel('ROUGE Metric')
            plt.ylabel('Score')
            plt.ylim(0, 1)

            plt.tight_layout()
            plt.savefig(f'visualizations/evaluation_scores_{self.direction}.png', bbox_inches='tight', dpi=300)
            plt.close()

            self.log_step("Evaluation scores visualized", "info")
        except Exception as e:
            self.log_step(f"Could not generate evaluation visualization: {str(e)}", "info")

    def generate_final_report(self):
        try:
            try:
                train_metrics = {
                    'loss': self.trainer.state.log_history[-1]['loss'] if hasattr(self.trainer, 'state') else 'N/A',
                    'time': self.trainer.state.log_history[-1]['train_runtime'] if hasattr(self.trainer,
                                                                                           'state') else 'N/A'
                }
            except (IndexError, KeyError, AttributeError):
                train_metrics = {'loss': 'N/A', 'time': 'N/A'}

            report = f"""
            ============== FINAL REPORT - {self.direction.upper()} ==============

            Pipeline executed on: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            Device used: {self.device}

            Dataset Statistics:
            - Training samples: {len(self.dataset['train']):,}
            - Validation samples: {len(self.dataset['validation']):,}

            Model Information:
            - Base model: {self.config['model_name']}
            - Vocabulary size: {self.tokenizer.vocab_size if hasattr(self, 'tokenizer') else 'N/A':,}
            - Fine-tuned model saved to: ./fine_tuned_model_{self.direction}/

            Training Results:
            - Final training loss: {train_metrics.get('loss', 'N/A')}
            - Training time: {train_metrics.get('time', 'N/A'):.0f} seconds

            Generated Files:
            - Fine-tuned model: ./fine_tuned_model_{self.direction}/
            - Training logs: {self.log_file}
            - Training metrics: training_metrics_{self.direction}.txt
            - Visualizations: ./visualizations/

            =====================================================
            """

            print(report)
            with open(f'final_report_{self.direction}.txt', 'w', encoding='utf-8') as f:
                f.write(report)
            self.log_step("Final report generated", "info")
        except Exception as e:
            self.log_step(f"Could not generate final report: {str(e)}", "info")

    def run_pipeline(self):
        try:
            self.log_step(f"Starting {self.direction} translation pipeline", "start")

            original_log_file = self.log_file
            self.log_file = "translation_log.txt"

            self.setup_resources()
            self.load_data()
            self.clean_data()
            self.filter_data()
            self.split_data()
            self.load_model()
            self.tokenize_data()
            self.setup_training()
            self.train_model()
            self.evaluate_translations()
            self.generate_final_report()

            self.log_file = original_log_file
            self.log_step(f"{self.direction} pipeline completed successfully", "success")
        except Exception as e:
            self.log_step(f"Pipeline failed: {str(e)}", "error")
            raise


if __name__ == "__main__":
    os.makedirs('visualizations', exist_ok=True)

    print("=" * 80)
    print("Bilingual Translation System with Tatoeba Dataset")
    print("=" * 80)

    # Initialize both pipelines
    ar_en_pipeline = BilingualTranslationPipeline('ar-en')
    en_ar_pipeline = BilingualTranslationPipeline('en-ar')

    # Run training pipelines
    print("\nRunning Arabic-to-English pipeline...")
    ar_en_pipeline.run_pipeline()

    print("\nRunning English-to-Arabic pipeline...")
    en_ar_pipeline.run_pipeline()

    # Interactive translation
    print("\n" + "=" * 80)
    print("Interactive Translation Mode")
    print("1. Arabic to English")
    print("2. English to Arabic")
    print("=" * 80)

    while True:
        choice = input("\nSelect direction (1/2) or 'exit' to quit: ")

        if choice.lower() == 'exit':
            break
        elif choice == '1':
            ar_en_pipeline.interactive_translation_test()
        elif choice == '2':
            en_ar_pipeline.interactive_translation_test()
        else:
            print("Invalid choice. Please enter 1, 2, or 'exit'")