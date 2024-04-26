import time

import torch
from datasets import Dataset
from tqdm import tqdm
from transformers import (
    GenerationConfig,
    TrainerCallback,
    TrainerControl,
    TrainerState,
    TrainingArguments,
)
from accelerate.utils import gather_object
from ..models import (
    unwrap_model_for_generation,
)

from ..import_utils import is_wandb_available

if is_wandb_available():
    import wandb

class WinRateCallback(TrainerCallback):
    def __init__(
        self,
        prompt_dataset: Dataset,
        generation_config: GenerationConfig,
        judge,
        trainer,
    ):
        self.prompt_dataset = prompt_dataset
        self.judge = judge
        self.generation_config = generation_config
        self.completions = []
        self.ref_completions = []
        self.trainer = trainer

    def on_train_begin(self, args: TrainingArguments, state: TrainerState, control: TrainerControl, **kwargs):
        model = self.trainer.model_wrapped
        tokenizer = kwargs["tokenizer"]
        accelerator = self.trainer.accelerator

        with accelerator.split_between_processes(self.prompt_dataset, apply_padding=True) as prompts:
            print(f"Num prompts: {len(prompts)} for device {accelerator.device}")
            print(prompts)
            print(prompts[0])
            with unwrap_model_for_generation(model, accelerator) as unwrapped_model:
                unwrapped_model.eval()
                # TODO: make this batched
                for messages in tqdm(prompts["chosen"], desc="Generating ref completions for win rate"):
                    print(f"Messages: {messages}")
                    if messages[-1]["role"] == "assistant":
                        messages = messages[:-1]

                    tokenized_prompt = tokenizer.apply_chat_template(
                        messages, tokenize=True, add_generation_prompt=True, return_tensors="pt"
                    ).to(model.device)
                    generation = unwrapped_model.generate(
                        tokenized_prompt,
                        generation_config=self.generation_config,
                    )
                    padded_prompt_length = tokenized_prompt.shape[1]
                    generation = generation[:, padded_prompt_length:]
                    text_generations = tokenizer.batch_decode(generation, skip_special_tokens=True)

                    ref_response = text_generations[0]
                    self.ref_completions.append(ref_response)
                unwrapped_model.train()

    def on_evaluate(self, args: TrainingArguments, state: TrainerState, control: TrainerControl, **kwargs):
        model = self.trainer.model_wrapped
        tokenizer = kwargs["tokenizer"]
        accelerator = self.trainer.accelerator

        with accelerator.split_between_processes(self.prompt_dataset, apply_padding=True) as prompts:
            local_dataset = Dataset.from_dict(prompts)

            annotation_batch = {"prompts": local_dataset["prompt"], "completions": []}

            with unwrap_model_for_generation(model, accelerator) as unwrapped_model:
                unwrapped_model.eval()
                for i, messages in enumerate(
                    tqdm(local_dataset["chosen"], desc="Generating completions for win rate")
                ):
                    if messages[-1]["role"] == "assistant":
                        messages = messages[:-1]
                    tokenized_prompt = tokenizer.apply_chat_template(
                        messages, tokenize=True, add_generation_prompt=True, return_tensors="pt"
                    ).to(model.device)
                    generations = unwrapped_model.generate(
                        tokenized_prompt,
                        generation_config=self.generation_config,
                    )
                    padded_prompt_length = tokenized_prompt.shape[1]
                    generations = generations[:, padded_prompt_length:]
                    text_generations = tokenizer.batch_decode(generations, skip_special_tokens=True)

                    response0 = text_generations[0]
                    response1 = self.ref_completions[i]

                    annotation_batch["completions"].append([response0, response1])
                unwrapped_model.train()
            # TODO, rerun with order or responses swapped and average
            results_dict = self.judge.judge_batch(annotation_batch["prompts"], annotation_batch["completions"])
            results_dict = Dataset.from_dict(
                {
                    "results": results_dict,
                    "prompts": annotation_batch["prompts"],
                    "completions": annotation_batch["completions"],
                }
            )  # maybe just map the original dataset for logging
            results_dict = gather_object(results_dict)

        if accelerator.is_main_process:
            dataset_len = len(self.prompt_dataset)
            results_dataset = Dataset.from_list(results_dict).select(range(dataset_len))

            win_rate = sum([r == 0 for r in results_dataset["results"]]) / len(results_dataset)
            self.trainer.log({"win_rate": win_rate})

            if is_wandb_available():
                wandb.log({"eval_win_rate": win_rate, "train/global_step": state.global_step})
                prompts = results_dataset["prompts"]
                policy = [c[0] for c in results_dataset["completions"]]
                ref = [c[1] for c in results_dataset["completions"]]
                chosen_indices = results_dataset["results"]

                self.trainer.log(
                    {
                        "winrate_generations": wandb.Table(
                            columns=["Prompt", "Policy", "Ref Model", "Chosen index"],
                            rows=[  # TODO replace with zip unpacking
                                [prompt, pol, ref, index]
                                for prompt, pol, ref, index in zip(prompts, policy, ref, chosen_indices)
                            ],
                        )
                    }
                )
                # pop Table otherwise it is included in the history which cannot be pickled and causes an error
                self.trainer.state.log_history.pop()