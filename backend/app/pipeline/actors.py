"""Actor-style FASTQ QC pipeline stages connected by asyncio queues."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


PHRED_OFFSET = 33
VALID_BASES = set("ACGTacgtNn")


class ActorError(Exception):
    """Raised when an actor fails its stage."""


@dataclass
class FastqRead:
    header: str
    sequence: str
    plus: str
    quality: str


@dataclass
class PipelineContext:
    """Shared mutable context passed between actors via queue messages."""

    fastq_text: str
    reads: list[FastqRead] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    failed_actor: str | None = None


@dataclass
class QueueMessage:
    ok: bool
    context: PipelineContext
    error: str | None = None


class ParseActor:
    name = "ParseActor"

    async def run(self, in_q: asyncio.Queue, out_q: asyncio.Queue) -> None:
        msg: QueueMessage = await in_q.get()
        if not msg.ok:
            await out_q.put(msg)
            return
        ctx = msg.context
        try:
            reads = self._parse(ctx.fastq_text)
            if not reads:
                raise ActorError("FASTQ 为空或不含完整读段")
            ctx.reads = reads
            ctx.metrics["reads"] = len(reads)
            await out_q.put(QueueMessage(ok=True, context=ctx))
        except ActorError as exc:
            ctx.error = str(exc)
            ctx.failed_actor = self.name
            await out_q.put(QueueMessage(ok=False, context=ctx, error=str(exc)))

    def _parse(self, text: str) -> list[FastqRead]:
        # Keep blank lines as structural errors for malformed files
        raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        # Drop trailing empty line from final newline
        if raw_lines and raw_lines[-1] == "":
            raw_lines = raw_lines[:-1]
        if not raw_lines:
            raise ActorError("FASTQ 内容为空")

        reads: list[FastqRead] = []
        i = 0
        while i < len(raw_lines):
            if i + 3 >= len(raw_lines):
                raise ActorError(
                    f"FASTQ 记录不完整：从第 {i + 1} 行起不足 4 行（畸形输入）"
                )
            header, seq, plus, qual = raw_lines[i], raw_lines[i + 1], raw_lines[i + 2], raw_lines[i + 3]
            if not header.startswith("@"):
                raise ActorError(f"第 {i + 1} 行表头必须以 @ 开头，实际为: {header[:40]!r}")
            if not plus.startswith("+"):
                raise ActorError(f"第 {i + 3} 行分隔符必须以 + 开头，实际为: {plus[:40]!r}")
            if len(seq) == 0:
                raise ActorError(f"第 {i + 2} 行序列为空")
            if len(seq) != len(qual):
                raise ActorError(
                    f"序列与质量串长度不一致：seq={len(seq)} qual={len(qual)}（读段 {header}）"
                )
            bad = [c for c in seq if c not in VALID_BASES]
            if bad:
                raise ActorError(f"序列含非法碱基 {bad[0]!r}（读段 {header}）")
            reads.append(FastqRead(header=header, sequence=seq, plus=plus, quality=qual))
            i += 4
        return reads


class QualityHistActor:
    name = "QualityHistActor"

    async def run(self, in_q: asyncio.Queue, out_q: asyncio.Queue) -> None:
        msg: QueueMessage = await in_q.get()
        if not msg.ok:
            await out_q.put(msg)
            return
        ctx = msg.context
        try:
            mean_q, per_pos, hist = self._compute(ctx.reads)
            ctx.metrics["mean_quality"] = mean_q
            ctx.metrics["per_position"] = per_pos
            ctx.metrics["quality_histogram"] = hist
            await out_q.put(QueueMessage(ok=True, context=ctx))
        except Exception as exc:  # noqa: BLE001
            err = f"质量统计失败: {exc}"
            ctx.error = err
            ctx.failed_actor = self.name
            await out_q.put(QueueMessage(ok=False, context=ctx, error=err))

    def _compute(self, reads: list[FastqRead]) -> tuple[float, list[dict], dict[str, int]]:
        if not reads:
            raise ActorError("无读段可计算质量")
        total_score = 0
        total_bases = 0
        max_len = max(len(r.quality) for r in reads)
        pos_sums = [0.0] * max_len
        pos_counts = [0] * max_len
        hist: dict[str, int] = {}

        for r in reads:
            for i, ch in enumerate(r.quality):
                score = ord(ch) - PHRED_OFFSET
                if score < 0 or score > 93:
                    raise ActorError(f"非法 Phred 字符 {ch!r}")
                total_score += score
                total_bases += 1
                pos_sums[i] += score
                pos_counts[i] += 1
                bucket = str(score)
                hist[bucket] = hist.get(bucket, 0) + 1

        mean_q = round(total_score / total_bases, 3) if total_bases else 0.0
        per_pos = [
            {"position": i + 1, "mean_quality": round(pos_sums[i] / pos_counts[i], 3)}
            for i in range(max_len)
            if pos_counts[i] > 0
        ]
        return mean_q, per_pos, hist


class NContentActor:
    name = "NContentActor"

    async def run(self, in_q: asyncio.Queue, out_q: asyncio.Queue) -> None:
        msg: QueueMessage = await in_q.get()
        if not msg.ok:
            await out_q.put(msg)
            return
        ctx = msg.context
        try:
            n_count = 0
            total = 0
            for r in ctx.reads:
                total += len(r.sequence)
                n_count += sum(1 for c in r.sequence if c in "Nn")
            n_rate = round(n_count / total, 6) if total else 0.0
            ctx.metrics["n_count"] = n_count
            ctx.metrics["n_rate"] = n_rate
            ctx.metrics["total_bases"] = total
            await out_q.put(QueueMessage(ok=True, context=ctx))
        except Exception as exc:  # noqa: BLE001
            err = f"N 含量统计失败: {exc}"
            ctx.error = err
            ctx.failed_actor = self.name
            await out_q.put(QueueMessage(ok=False, context=ctx, error=err))


class ReportActor:
    name = "ReportActor"

    async def run(self, in_q: asyncio.Queue, out_q: asyncio.Queue) -> None:
        msg: QueueMessage = await in_q.get()
        if not msg.ok:
            await out_q.put(msg)
            return
        ctx = msg.context
        try:
            report = {
                "reads": ctx.metrics.get("reads"),
                "mean_quality": ctx.metrics.get("mean_quality"),
                "n_rate": ctx.metrics.get("n_rate"),
                "n_count": ctx.metrics.get("n_count"),
                "total_bases": ctx.metrics.get("total_bases"),
                "per_position_summary": {
                    "positions": len(ctx.metrics.get("per_position") or []),
                    "first5": (ctx.metrics.get("per_position") or [])[:5],
                    "last5": (ctx.metrics.get("per_position") or [])[-5:],
                },
                "quality_histogram_top": dict(
                    sorted(
                        (ctx.metrics.get("quality_histogram") or {}).items(),
                        key=lambda kv: int(kv[0]),
                    )[:10]
                ),
            }
            ctx.metrics["report"] = report
            # Flatten key metrics for API convenience
            ctx.metrics["summary"] = {
                "reads": report["reads"],
                "mean_quality": report["mean_quality"],
                "n_rate": report["n_rate"],
                "per_position": report["per_position_summary"],
            }
            await out_q.put(QueueMessage(ok=True, context=ctx))
        except Exception as exc:  # noqa: BLE001
            err = f"报告汇总失败: {exc}"
            ctx.error = err
            ctx.failed_actor = self.name
            await out_q.put(QueueMessage(ok=False, context=ctx, error=err))


ACTOR_CHAIN = [ParseActor, QualityHistActor, NContentActor, ReportActor]
