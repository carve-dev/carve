#!/usr/bin/env bash
# Stop-hook tripwire — the phase-free design corpus must never contain Carve
# version/phase vocabulary (v0.1, v0.2, post-v0.1, ...). Phasing is increment-
# relative and lives only in DELIVERY.md. See:
#   specs/_strategy/2026-06-change-lifecycle.md
#   specs/_strategy/2026-06-spec-structure.md
#
# Scope is intentionally narrow — only the docs that must read phase-free:
#   capabilities/, PRD.md, ARCHITECTURE.md, use-cases.md
# Excluded (they legitimately contain v0.x): _strategy/ (ADRs discuss the
# anti-pattern), reference/ (the generic v0.x.y release-tag template + the
# SemVer scheme), _archive/ and milestone-*/ (historical shipped tags).
#
# On a hit: print the offenders to stderr and exit 2, which blocks the turn
# from ending until they're removed. Clean -> exit 0 (silent).

root="${CLAUDE_PROJECT_DIR:-$(pwd)}"

targets=(
  "$root/specs/capabilities"
  "$root/specs/PRD.md"
  "$root/specs/ARCHITECTURE.md"
  "$root/specs/use-cases.md"
)

hits=$(grep -rnE 'v0\.[0-9x]|post-v0' "${targets[@]}" 2>/dev/null || true)

if [ -n "$hits" ]; then
  {
    echo "BLOCKED: Carve version/phase vocabulary found in the phase-free corpus."
    echo "Capability specs, PRD, ARCHITECTURE, and use-cases are phase-free — remove these."
    echo "Phasing is increment-relative and belongs only in DELIVERY.md."
    echo "(See specs/_strategy/2026-06-change-lifecycle.md.)"
    echo
    echo "$hits"
  } >&2
  exit 2
fi

exit 0
