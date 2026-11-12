"""Measure synchronous OpenRouter embeddings throughput; requests incur API costs."""

import argparse
import asyncio
import csv
import json
import math
import os
from pathlib import Path
import random
import statistics
import time
from datetime import datetime, timezone

import httpx


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def load_texts(args):
    if args.input:
        texts = json.loads(Path(args.input).read_text(encoding="utf-8-sig"))
        if not isinstance(texts, list) or not texts or not all(
            isinstance(text, str) and text.strip() for text in texts
        ):
            raise ValueError("Input must be a nonempty JSON array of nonempty strings")
        return texts
    topics = [
        "Семантический поиск сопоставляет векторы запроса и фрагментов научных статей.",
        "Квантизация весов нейронной сети позволяет уменьшить потребление памяти.",
        "Индексация документов включает извлечение текста и разбиение на чанки.",
        "Производительность системы зависит от размера пакета и числа параллельных запросов.",
    ]
    return [
        (f"Исследование {index}. " + " ".join(
            topics[(index + offset) % len(topics)] for offset in range(args.sentences)
        ))
        for index in range(args.chunks)
    ]


async def experiment(client, args, texts, batch_size, concurrency, repeat, raw):
    batches = [texts[index:index + batch_size] for index in range(0, len(texts), batch_size)]
    pending = iter(enumerate(batches))
    records = []

    async def worker():
        for batch_index, batch in pending:
            started = time.perf_counter()
            record = {
                "batch_size": batch_size, "concurrency": concurrency, "repeat": repeat,
                "batch_index": batch_index, "chunks": len(batch), "status": None,
                "ok": False, "error": None,
                "started_utc": datetime.now(timezone.utc).isoformat(),
                "started_offset_seconds": started - experiment_started,
            }
            try:
                response = await client.post("embeddings", json={
                    "model": args.model, "input": batch, "encoding_format": "float",
                    "provider": {
                        "only": ["deepinfra"],
                        "allow_fallbacks": False,
                    },
                })
                record["status"] = response.status_code
                if not response.is_success:
                    record["error"] = f"HTTP {response.status_code}"
                    record["retry_after"] = response.headers.get("retry-after")
                    record["error_headers"] = {
                        name: value for name, value in response.headers.items()
                        if name.startswith("x-ratelimit-") or name in {
                            "retry-after", "x-request-id", "x-openrouter-request-id", "cf-ray",
                        }
                    }
                    try:
                        record["response_body"] = response.json()
                    except ValueError:
                        record["response_body"] = response.text
                else:
                    data = response.json()
                    items = sorted(data["data"], key=lambda item: item["index"])
                    if [item["index"] for item in items] != list(range(len(batch))):
                        raise ValueError("Unexpected embedding indices/count")
                    dimensions = len(items[0]["embedding"])
                    if not dimensions or any(
                        len(item["embedding"]) != dimensions or not all(
                            isinstance(value, (int, float)) and math.isfinite(value)
                            for value in item["embedding"]
                        ) for item in items
                    ):
                        raise ValueError("Invalid embedding vectors")
                    record.update(ok=True, dimensions=dimensions, usage=data.get("usage"),
                                  response_model=data.get("model"), provider=data.get("provider"))
            except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as error:
                record["error"] = type(error).__name__
            record["seconds"] = time.perf_counter() - started
            record["finished_utc"] = datetime.now(timezone.utc).isoformat()
            records.append(record)
            raw.write(json.dumps(record, ensure_ascii=False) + "\n")
            raw.flush()

    experiment_started = started = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    elapsed = time.perf_counter() - started
    successful = [record for record in records if record["ok"]]
    latencies = [record["seconds"] for record in successful]
    good_chunks = sum(record["chunks"] for record in successful)
    return {
        "batch_size": batch_size, "concurrency": concurrency, "repeat": repeat,
        "requests": len(records), "failed_requests": len(records) - len(successful),
        "successful_chunks": good_chunks, "total_chunks": len(texts),
        "elapsed_seconds": elapsed, "chunks_per_second": good_chunks / elapsed,
        "request_median_seconds": statistics.median(latencies) if latencies else None,
        "request_p95_seconds": percentile(latencies, .95) if latencies else None,
    }


async def main(args):
    texts = load_texts(args)
    cases = [(batch, parallel, repeat)
             for repeat in range(1, args.repeats + 1)
             for batch in args.batch_sizes for parallel in args.concurrency]
    random.Random(args.seed).shuffle(cases)
    request_count = sum(math.ceil(len(texts) / batch) for batch, _, _ in cases)
    print(f"Model: {args.model}; chunks: {len(texts)}; cases: {len(cases)}; "
          f"requests: {request_count}; total chunk submissions: {len(texts) * len(cases)}", flush=True)
    if args.dry_run:
        print("Dry run: no API calls, no output files.")
        return
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise ValueError("OPENROUTER_API_KEY is required")
    output = Path(args.output) / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True, exist_ok=False)
    metadata = {**vars(args), "input_chunks": len(texts),
                "input_characters": sum(map(len, texts)), "started_utc": datetime.now(timezone.utc).isoformat()}
    (output / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    results = []
    async with httpx.AsyncClient(
        base_url="https://openrouter.ai/api/v1/",
        headers={"Authorization": f"Bearer {key}"}, timeout=args.timeout,
        limits=httpx.Limits(max_connections=max(args.concurrency),
                           max_keepalive_connections=max(args.concurrency)),
    ) as client:
        with (output / "requests.jsonl").open("w", encoding="utf-8") as raw, \
                (output / "summary.csv").open("w", newline="", encoding="utf-8") as summary:
            writer = None
            for index, (batch, parallel, repeat) in enumerate(cases):
                result = await experiment(client, args, texts, batch, parallel, repeat, raw)
                results.append(result)
                if writer is None:
                    writer = csv.DictWriter(summary, fieldnames=list(result))
                    writer.writeheader()
                writer.writerow(result)
                summary.flush()
                print(f"[{index + 1}/{len(cases)}] batch={batch} parallel={parallel} "
                      f"repeat={repeat}: {result['chunks_per_second']:.2f} chunks/s; "
                      f"failed={result['failed_requests']}/{result['requests']}", flush=True)
                if index + 1 < len(cases):
                    await asyncio.sleep(args.cooldown)
    (output / "summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Results: {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=os.getenv("EMBEDDING_OPENROUTER_MODEL", "baai/bge-m3"))
    parser.add_argument("--provider", choices=["deepinfra"], default="deepinfra",
                        help="Only DeepInfra is supported; fallback is disabled")
    parser.add_argument("--batch-sizes", nargs="+", type=positive, default=[8, 16, 32, 64, 128])
    parser.add_argument("--concurrency", nargs="+", type=positive, default=[1, 2, 3, 4])
    parser.add_argument("--repeats", type=positive, default=3)
    parser.add_argument("--chunks", type=positive, default=960)
    parser.add_argument("--sentences", type=positive, default=12)
    parser.add_argument("--input", help="JSON file containing an array of chunk strings")
    parser.add_argument("--output", default="results")
    parser.add_argument("--timeout", type=positive, default=60)
    parser.add_argument("--cooldown", type=positive, default=3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--dry-run", action="store_true")
    try:
        asyncio.run(main(parser.parse_args()))
    except (ValueError, OSError) as error:
        parser.exit(1, f"Error: {error}\n")
