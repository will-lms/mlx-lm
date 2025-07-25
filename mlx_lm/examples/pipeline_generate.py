# Copyright © 2024 Apple Inc.

"""
Run with:

```
mlx.launch \
 --hostfile /path/to/hosts.json \
 /path/to/pipeline_generate.py \
 --prompt "hello world"
```

Make sure you can run MLX over MPI on two hosts. For more information see the
documentation:

https://ml-explore.github.io/mlx/build/html/usage/distributed.html).
"""

import argparse
import json
import resource
from pathlib import Path

import mlx.core as mx
from huggingface_hub import snapshot_download
from mlx.utils import tree_flatten

from mlx_lm import load, stream_generate
from mlx_lm.utils import load_model, load_tokenizer

# Needed for 8 bit model
resource.setrlimit(resource.RLIMIT_NOFILE, (2048, 4096))


def download(repo: str, allow_patterns: list[str]) -> Path:
    return Path(
        snapshot_download(
            repo,
            allow_patterns=allow_patterns,
        )
    )


def shard_and_load(repo):
    # Get model path with everything but weight safetensors
    model_path = download(
        args.model,
        allow_patterns=["*.json", "*.py", "tokenizer.model", "*.tiktoken", "*.txt"],
    )

    # Lazy load and shard model to figure out
    # which weights we need
    model, config = load_model(model_path, lazy=True, strict=False)

    group = mx.distributed.init()
    rank = group.rank()
    model.model.pipeline(group)

    # Figure out which files we need for the local shard
    with open(model_path / "model.safetensors.index.json", "r") as fid:
        weight_index = json.load(fid)["weight_map"]

    local_files = set()
    for k, _ in tree_flatten(model.parameters()):
        local_files.add(weight_index[k])

    # Download weights for local shard
    download(args.model, allow_patterns=local_files)

    # Load and shard the model, and load the weights
    tokenizer = load_tokenizer(
        model_path,
        {"trust_remote_code": True},
        eos_token_ids=config.get("eos_token_id", None),
    )
    model, _ = load_model(model_path, lazy=True, strict=False)
    model.model.pipeline(group)
    mx.eval(model.parameters())

    # Synchronize processes before generation to avoid timeout if downloading
    # model for the first time.
    mx.eval(mx.distributed.all_sum(mx.array(1.0), stream=mx.cpu))
    return model, tokenizer


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LLM pipelined inference example")
    parser.add_argument(
        "--model",
        default="mlx-community/DeepSeek-R1-3bit",
        help="HF repo or path to local model.",
    )
    parser.add_argument(
        "--prompt",
        "-p",
        default="Write a quicksort in C++.",
        help="Message to be processed by the model ('-' reads from stdin)",
    )
    parser.add_argument(
        "--max-tokens",
        "-m",
        type=int,
        default=256,
        help="Maximum number of tokens to generate",
    )
    args = parser.parse_args()

    group = mx.distributed.init()
    rank = group.rank()

    def rprint(*args, **kwargs):
        if rank == 0:
            print(*args, **kwargs)

    model, tokenizer = shard_and_load(args.model)

    messages = [{"role": "user", "content": args.prompt}]
    prompt = tokenizer.apply_chat_template(messages, add_generation_prompt=True)

    for response in stream_generate(
        model, tokenizer, prompt, max_tokens=args.max_tokens
    ):
        rprint(response.text, end="", flush=True)

    rprint()
    rprint("=" * 10)
    rprint(
        f"Prompt: {response.prompt_tokens} tokens, "
        f"{response.prompt_tps:.3f} tokens-per-sec"
    )
    rprint(
        f"Generation: {response.generation_tokens} tokens, "
        f"{response.generation_tps:.3f} tokens-per-sec"
    )
    rprint(f"Peak memory: {response.peak_memory:.3f} GB")
