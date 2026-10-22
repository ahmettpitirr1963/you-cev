"""Sunucu iş kuyruğu testleri (öne alma, çoklu izleyici, cümle birleştirme).
Çalıştır: .venv\\Scripts\\python tests\\test_jobs.py"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from jobs import Job, JobManager  # noqa: E402
from pipeline.segmenter import segment  # noqa: E402


def fresh():
    job = Job("testvid0000", "abcd1234", "google", "youtube", "testvid0000")
    job.duration = 600
    job.load_bounds()  # sınırlar: 0,20,80,140,200,260,320,...
    for c in job.chunks[:3]:
        c.state = "done"
    return job


async def preempt_tests():
    m = JobManager()

    async def fake():
        await asyncio.sleep(10)

    def case(job, running_idx, viewer, pos, prev_pos=None):
        if prev_pos is not None:
            job.viewers[viewer] = prev_pos
        task = asyncio.ensure_future(fake())
        job.chunks[running_idx].state = "running"
        m._current = (job, job.chunks[running_idx], task)
        m.set_position(job, pos, viewer=viewer)
        r = task.cancelling() > 0
        task.cancel()
        job.chunks[running_idx].state = "pending"
        return r

    checks = [
        ("yakında gerekli parça işleniyor", case(fresh(), 3, 1, 30), False),
        ("uzağa sarıldı", case(fresh(), 3, 1, 300), True),
        ("bulunduğu parça bekliyor, sonraki işleniyor", case(fresh(), 4, 1, 150), True),
        ("bulunduğu parça hazır, sonraki işleniyor", case(fresh(), 3, 1, 100), False),
        ("geriye (hazır yere) sarıldı", case(fresh(), 5, 1, 30), False),
        ("normal oynatma (sıçrama yok) bırakmaz", case(fresh(), 3, 1, 301, prev_pos=300), False),
    ]
    j = fresh()
    j.chunks[0].state = "pending"
    j.viewers[1] = 10.0
    checks.append(("iki izleyici: A'nın parçası B yüzünden bırakılmaz", case(j, 0, 2, 400), False))
    j = fresh()
    j.viewers[1] = 10.0
    j.viewers[2] = 400.0
    checks.append(("iki izleyici: en acil parça seçilir", j.next_chunk().idx == j.chunk_at(400), True))
    return checks


def segment_tests():
    ws = [{"w": " The", "s": 0, "e": .2}, {"w": " light", "s": .2, "e": .4}, {"w": "-sensitive", "s": .4, "e": .7},
          {"w": " cells", "s": .7, "e": 1}, {"w": " fire.", "s": 1, "e": 1.3}, {"w": " Okay", "s": 3.5, "e": 3.8}]
    out = segment(ws)
    return [
        ("tireli kelime birleşir", out[0]["text"] == "The light-sensitive cells fire.", True),
        ("noktalı cümle tamam sayılır", out[0]["complete"], True),
        ("sondaki yarım cümle bekletilir", out[-1]["complete"], False),
    ]


def main():
    checks = asyncio.run(preempt_tests()) + segment_tests()
    failed = 0
    for name, got, want in checks:
        ok = got == want
        failed += not ok
        print(("OK  " if ok else "HATA"), name)
    print("TÜM TESTLER GEÇTİ" if not failed else f"{failed} TEST BAŞARISIZ")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
