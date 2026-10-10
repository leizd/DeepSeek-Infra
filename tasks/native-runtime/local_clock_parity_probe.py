"""OS-local clock/zone parity probe, Python side.

`format_current_time_context` renders `Local time: <iso> (<name>)` into every prompt, and the
name is not a constant — it is whatever the machine's zone is called *in the machine's own
locale*. The Rust side resolves it with `deepseek_gateway::local_clock`; this side resolves it
the way CPython does (`datetime.fromtimestamp(epoch).astimezone()`), and both render the same
pinned instant through the oracle's own formatter, so the two outputs compare byte for byte.

**The instant is pinned, the zone is not.** The two sides run seconds apart, so each one's own
`now` would differ and the diff would be non-empty for a reason that has nothing to do with the
zone. The epoch is captured once here and handed to the Rust side, which resolves *its own*
offset and name for it: the values under test are still the host's, and the byte comparison is
still the check.

**What this can fail on**: a wrong offset sign (Windows `Bias` is minutes *west* of UTC), a
missing bias term, the daylight name chosen for standard time or the reverse, a name taken from
anywhere but the C library — or a hand-written table of "obvious" zone names, which is exactly
the failure mode this probe exists for: on a zh-CN Windows the real name is `中国标准时间`, not
`China Standard Time`.

`is_daylight` is a diagnostic, not part of the rendered contract: it is what separates "the
name is wrong" from "the period is wrong", and the two sides classify the period through
different routes (`time.localtime().tm_isdst` versus the API's own return value), so agreement
there is worth knowing about rather than assuming.

Usage::

    python tasks/native-runtime/local_clock_parity_probe.py > python.json
    EPOCH=$(python -c "import json;print(json.load(open('python.json'))['epoch'])")
    cd rust && cargo run -p deepseek-gateway --example local_clock_parity_probe -- "$EPOCH" > ../rust.json
    cd .. && diff <(tr -d '\\r' < python.json) <(tr -d '\\r' < rust.json)
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from deepseek_infra.infra.gateway import deepseek_client as dc  # noqa: E402


def main() -> int:
    epoch = int(time.time())
    local = datetime.fromtimestamp(epoch).astimezone()
    offset = local.utcoffset()
    if offset is None:
        raise SystemExit("the host resolved no UTC offset for the local time")

    out = {
        "epoch": epoch,
        "is_daylight": int(time.localtime(epoch).tm_isdst) == 1,
        "offset_seconds": int(offset.total_seconds()),
        "rendered": dc.format_current_time_context(local),
        "tzname": str(local.tzname() or ""),
    }
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
