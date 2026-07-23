**What & why**

**Checklist**
- [ ] `python3 -m unittest discover -s tests` green locally
- [ ] No new runtime dependencies (loops are stdlib-only by design)
- [ ] NDJSON readers still tolerate partial trailing lines (if touched)
- [ ] `hooks/peer_hook.py` still fails open (if touched)
- [ ] Pure modules (`hooks/logic.py`, `codecouncil/signal_filter.py`) still do no I/O (if touched)
- [ ] New/changed behavior has a test that fails without the change
