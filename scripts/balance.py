#!/usr/bin/env python3
"""Rebalance OMP model roles from each provider's live quota pools.

Provider ids come from `omp usage --json` and the model catalog. Nothing in
here names a provider. A pool covers a model when its label or id names that
model's family; otherwise an unlabeled "other"/"api" pool covers families that
some provider meters by name, and an unlabeled "models"/"auto" pool covers the
rest. Roles then follow whichever provider still has room for that family.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_TOKEN = re.compile(r"[^a-z0-9]+")
_FAMILY = {
    "gemini": "gemini",
    "claude": "claude",
    "anthropic": "claude",
    "gpt": "gpt",
    "openai": "gpt",
    "grok": "grok",
    "composer": "composer",
    "opus": "opus",
    "sonnet": "sonnet",
}
_WINDOW_MS = {"5h": 18_000_000, "weekly": 604_800_000, "monthly": 2_592_000_000}
_PERCENT_FULL = 0.90
_USD_FULL = 0.95


def agent_dir():
    """Active OMP agent directory. Never a machine-specific path."""
    override = os.environ.get("PI_CODING_AGENT_DIR")
    if override:
        return Path(override).expanduser()
    res = subprocess.run(["omp", "config", "path"], capture_output=True, text=True)
    lines = [line.strip() for line in (res.stdout or "").splitlines() if line.strip()]
    if res.returncode == 0 and lines:
        return Path(lines[-1]).expanduser()
    return Path("~/.omp/agent").expanduser()


def config_path():
    base = agent_dir()
    yml = base / "config.yml"
    alt = base / "config.yaml"
    if alt.exists() and not yml.exists():
        return alt
    return yml


def load_catalog():
    """Models the host already imported, or `omp models --json` from a CLI run.

    The extension writes the in-process catalog to stdin during session_start,
    before retry.fallbackChains validation. A selector that is not in this
    snapshot is not written.
    """
    if not sys.stdin.isatty():
        raw = sys.stdin.read()
        if raw.strip():
            data = json.loads(raw)
            models = data.get("models", data if isinstance(data, list) else [])
            if models:
                return [_normalize_model(m) for m in models if m.get("provider") and m.get("id")]
    res = subprocess.run(
        ["omp", "models", "--json"],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(res.stdout)
    return [_normalize_model(m) for m in data.get("models", []) if m.get("provider") and m.get("id")]


def _normalize_model(model):
    thinking = model.get("thinking")
    if isinstance(thinking, list):
        thinking = {"efforts": thinking}
    elif not isinstance(thinking, dict):
        thinking = {}
    return {
        "provider": model["provider"],
        "id": model["id"],
        "name": model.get("name") or model["id"],
        "identity": model.get("identity") or {},
        "thinking": thinking,
    }


def _parts(text):
    return {part for part in _TOKEN.split((text or "").lower()) if part}


def _revision(model):
    rev = (model.get("identity") or {}).get("revision")
    if rev:
        parts = []
        for piece in str(rev).split("."):
            parts.append(int(piece) if piece.isdigit() else 0)
        return tuple(parts)
    return tuple(int(n) for n in re.findall(r"\d+", model["id"])[:3])


def _logical_rank(model):
    thinking = model.get("thinking") or {}
    logical = 1 if thinking.get("efforts") or thinking.get("effortRouting") else 0
    return (_revision(model), logical, -len(model["id"]))


def model_families(model):
    ident = model.get("identity") or {}
    found = set()
    family = str(ident.get("family") or "").lower()
    if family in ("opus", "sonnet", "haiku"):
        found.add("claude")
        found.add(family)
    elif family:
        found.add(_FAMILY.get(family, family))
    mid = model["id"].lower()
    if "claude" in mid or "anthropic" in mid:
        found.add("claude")
    if "opus" in mid:
        found.update(("claude", "opus"))
    if "sonnet" in mid:
        found.update(("claude", "sonnet"))
    for token, canonical in _FAMILY.items():
        if token in ("opus", "sonnet", "claude", "anthropic"):
            continue
        if token in mid:
            found.add(canonical)
    return found


def _provider_models(catalog, provider):
    if not provider:
        return []
    return [m for m in catalog if m["provider"] == provider]


def _best(models):
    return max(models, key=_logical_rank) if models else None


def _groks(catalog, provider):
    pool = []
    for model in _provider_models(catalog, provider):
        ident = model.get("identity") or {}
        grok = ident.get("family") == "grok" or "grok" in model["id"]
        if not grok or model["id"].endswith("-fast") or "500k" in model["id"]:
            continue
        pool.append(model)
    pool.sort(key=_logical_rank, reverse=True)
    return pool


def _composer(catalog, provider, fast):
    pool = [
        m for m in _provider_models(catalog, provider)
        if "composer" in m["id"] and m["id"].endswith("-fast") == fast
    ]
    return _best(pool)


def _flash(catalog, provider):
    pool = []
    for model in _provider_models(catalog, provider):
        mid = model["id"]
        families = model_families(model)
        if "gemini" not in families and "gemini" not in mid:
            continue
        if "flash" not in mid:
            continue
        if "lite" in mid or "image" in mid:
            continue
        pool.append(model)
    return _best(pool)


def _claude(catalog, provider, family):
    pool = []
    for model in _provider_models(catalog, provider):
        ident = model.get("identity") or {}
        mid = model["id"]
        if ident.get("family") != family and f"claude-{family}" not in mid:
            continue
        if mid.endswith("-fast") or "-1m" in mid:
            continue
        pool.append(model)
    return _best(pool)


def _older_claude(catalog, provider, family, newest):
    if newest is None:
        return None
    pool = []
    for model in _provider_models(catalog, provider):
        ident = model.get("identity") or {}
        mid = model["id"]
        if model["id"] == newest["id"]:
            continue
        if ident.get("family") != family and f"claude-{family}" not in mid:
            continue
        if mid.endswith("-fast") or "-1m" in mid:
            continue
        if _revision(model) >= _revision(newest):
            continue
        pool.append(model)
    return _best(pool)


def selector(model, effort=None):
    """Catalog id, plus `:effort` only when that model actually advertises it."""
    if model is None:
        return None
    base = f"{model['provider']}/{model['id']}"
    thinking = model.get("thinking") or {}
    efforts = list(thinking.get("efforts") or [])
    routing = thinking.get("effortRouting") or {}
    if not effort:
        return base
    if model["id"].endswith(f"-{effort}") or model["id"].endswith(f":{effort}"):
        return base
    if effort in efforts or effort in routing:
        return f"{base}:{effort}"
    return base


def chain(*items):
    out = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


def get_usage():
    try:
        res = subprocess.run(
            ["omp", "usage", "--json"],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(res.stdout)
    except Exception as e:
        print(f"Error fetching usage: {e}", file=sys.stderr)
        return None


def _used_fraction(limit):
    """Same precedence as OMP `resolveUsedFraction` (0..1)."""
    amount = limit.get("amount") or {}
    if amount.get("usedFraction") is not None:
        return float(amount["usedFraction"])
    used = amount.get("used")
    limit_value = amount.get("limit")
    if used is not None and limit_value not in (None, 0):
        return float(used) / float(limit_value)
    if amount.get("unit") == "percent" and used is not None:
        return float(used) / 100.0
    remaining = amount.get("remainingFraction")
    if remaining is not None:
        return max(0.0, 1.0 - float(remaining))
    return 0.0


def _pool_families(limit):
    tokens = _parts(limit.get("label")) | _parts(limit.get("id"))
    found = set()
    for token, canonical in _FAMILY.items():
        if token in tokens:
            found.add(canonical)
    if "opus" in found or "sonnet" in found:
        found.add("claude")
    return found


def _pool_kind(limit, families):
    if families:
        return "named"
    tokens = _parts(limit.get("label")) | _parts(limit.get("id"))
    if "other" in tokens or "api" in tokens or "3p" in tokens:
        return "metered"
    if "auto" in tokens or "included" in tokens or "models" in tokens:
        return "included"
    return "general"


def _window_span(pool):
    if pool.get("durationMs"):
        return pool["durationMs"]
    return _WINDOW_MS.get(pool.get("window") or "", 0)


def analyze_quotas(data):
    if not data or "reports" not in data:
        return None

    providers = []
    for report in data.get("reports") or []:
        provider_id = report.get("provider")
        if not provider_id:
            continue
        grouped = {}
        order = []
        for limit in report.get("limits") or []:
            scope = limit.get("scope") or {}
            key = scope.get("sharedGroup") or limit.get("id")
            if key not in grouped:
                grouped[key] = []
                order.append(key)
            grouped[key].append(limit)

        pools = []
        for key in order:
            limits = grouped[key]
            families = set()
            for limit in limits:
                families |= _pool_families(limit)
            primary = max(limits, key=_used_fraction)
            amount = primary.get("amount") or {}
            window = primary.get("window") or {}
            scope = primary.get("scope") or {}
            pools.append({
                "id": primary.get("id") or key,
                "label": primary.get("label") or primary.get("id") or key,
                "kind": _pool_kind(primary, families),
                "families": sorted(families),
                "window": window.get("id") or scope.get("windowId"),
                "windowLabel": window.get("label"),
                "durationMs": window.get("durationMs"),
                "resetsAt": window.get("resetsAt"),
                "usedFraction": _used_fraction(primary),
                "unit": amount.get("unit") or "unknown",
                "used": amount.get("used"),
                "limit": amount.get("limit"),
            })
        providers.append({"id": provider_id, "pools": pools})

    return {"providers": providers}


def explicit_families(analysis):
    found = set()
    for provider in analysis.get("providers") or []:
        for pool in provider["pools"]:
            if pool["kind"] == "named":
                found.update(pool["families"])
    return found


def covering_pools(provider, model, explicit):
    """Pools on this provider that meter this model."""
    families = model_families(model)
    named = [
        pool for pool in provider["pools"]
        if pool["kind"] == "named" and families.intersection(pool["families"])
    ]
    if named:
        return named
    if families & explicit:
        metered = [pool for pool in provider["pools"] if pool["kind"] == "metered"]
        if metered:
            return metered
    else:
        included = [pool for pool in provider["pools"] if pool["kind"] == "included"]
        if included:
            return included
    general = [pool for pool in provider["pools"] if pool["kind"] == "general"]
    if general:
        return general
    return list(provider["pools"])


def pool_pressure(pools):
    if not pools:
        return 1.0
    return max(pool["usedFraction"] or 0.0 for pool in pools)


def pool_available(pools, percent_limit=_PERCENT_FULL, usd_limit=_USD_FULL):
    if not pools:
        return False
    for pool in pools:
        frac = pool["usedFraction"] or 0.0
        limit = usd_limit if pool["unit"] == "usd" else percent_limit
        if frac >= limit:
            return False
    return True


def _by_provider(analysis):
    return {provider["id"]: provider for provider in analysis.get("providers") or []}


def _pick_grok(catalog, providers, explicit):
    found = []
    for provider in providers:
        groks = _groks(catalog, provider["id"])
        if not groks:
            continue
        best = groks[0]
        previous = next((item for item in groks[1:] if _revision(item) < _revision(best)), None)
        pools = covering_pools(provider, best, explicit)
        found.append((pool_pressure(pools), _revision(best), best, previous, provider, pools))
    if not found:
        return None
    found.sort(key=lambda item: (item[0], tuple(-part for part in item[1])))
    _pressure, _rev, best, previous, provider, pools = found[0]
    return {"model": best, "previous": previous, "provider": provider, "pools": pools, "pressure": _pressure}


def _pick_composer(catalog, providers, explicit, fast):
    found = []
    for provider in providers:
        model = _composer(catalog, provider["id"], fast)
        if not model:
            continue
        pools = covering_pools(provider, model, explicit)
        found.append((pool_pressure(pools), _revision(model), model, provider, pools))
    if not found:
        return None
    found.sort(key=lambda item: (item[0], tuple(-part for part in item[1])))
    _pressure, _rev, model, provider, pools = found[0]
    return {"model": model, "provider": provider, "pools": pools}


def _pick_flash(catalog, providers, explicit):
    found = []
    for provider in providers:
        model = _flash(catalog, provider["id"])
        if not model:
            continue
        pools = covering_pools(provider, model, explicit)
        named = 0 if any(pool["kind"] == "named" for pool in pools) else 1
        found.append((named, pool_pressure(pools), _revision(model), model, provider, pools))
    if not found:
        return None
    found.sort(key=lambda item: (item[0], item[1], tuple(-part for part in item[2])))
    _named, _pressure, _rev, model, provider, pools = found[0]
    return {"model": model, "provider": provider, "pools": pools}


def _claude_candidates(catalog, providers, explicit):
    found = []
    for provider in providers:
        opus = _claude(catalog, provider["id"], "opus")
        sonnet = _claude(catalog, provider["id"], "sonnet")
        anchor = opus or sonnet
        if anchor is None:
            continue
        pools = covering_pools(provider, anchor, explicit)
        span = max((_window_span(pool) for pool in pools), default=0)
        found.append({
            "provider": provider,
            "opus": opus,
            "sonnet": sonnet,
            "opus_prev": _older_claude(catalog, provider["id"], "opus", opus),
            "sonnet_prev": _older_claude(catalog, provider["id"], "sonnet", sonnet),
            "pools": pools,
            "pressure": pool_pressure(pools),
            "available": pool_available(pools),
            "span": span,
            "named": bool(pools) and all(pool["kind"] == "named" for pool in pools),
        })
    found.sort(key=lambda item: (0 if item["available"] else 1, item["pressure"], -item["span"]))
    return found


def _pick_claude(catalog, providers, explicit):
    found = _claude_candidates(catalog, providers, explicit)
    return found[0] if found else None


def _fmt_amount(pool):
    frac = (pool["usedFraction"] or 0.0) * 100.0
    if pool["unit"] == "usd" and pool.get("limit") is not None:
        used = float(pool.get("used") or 0.0)
        limit = float(pool["limit"])
        if used >= 10 or limit >= 10:
            return f"${used:.0f}/${limit:.0f}"
        return f"${used:.2f}/${limit:.2f}"
    if frac >= 10:
        return f"{frac:.0f}%"
    return f"{frac:.1f}%"


def _reset_label(pool):
    resets_at = pool.get("resetsAt")
    if not resets_at:
        return None
    delta = max(0.0, (resets_at / 1000) - datetime.now(timezone.utc).timestamp())
    if delta >= 86400:
        return f"{delta / 86400:.1f}d"
    return f"{delta / 3600:.1f}h"


def _representative_pools(provider):
    chosen = {}
    for pool in provider["pools"]:
        current = chosen.get(pool["label"])
        if current is None:
            chosen[pool["label"]] = pool
            continue
        if (pool["usedFraction"] or 0) > (current["usedFraction"] or 0) + 1e-9:
            chosen[pool["label"]] = pool
        elif abs((pool["usedFraction"] or 0) - (current["usedFraction"] or 0)) <= 1e-9:
            if _window_span(pool) < _window_span(current):
                chosen[pool["label"]] = pool
    return list(chosen.values())


def pressure_level(analysis):
    hot = False
    warn = False
    for provider in analysis.get("providers") or []:
        for pool in provider["pools"]:
            frac = pool["usedFraction"] or 0.0
            full = _USD_FULL if pool["unit"] == "usd" else _PERCENT_FULL
            warn_at = 0.80
            if frac >= full:
                hot = True
            elif frac >= warn_at:
                warn = True
    if hot:
        return "hot"
    if warn:
        return "warn"
    return "ok"


def summary_line(analysis):
    parts = []
    for provider in analysis.get("providers") or []:
        for pool in _representative_pools(provider):
            parts.append(f"{provider['id']} {pool['label']} {_fmt_amount(pool)}")
    return " · ".join(parts)


def panel_lines(analysis, selected_name, profile, applied):
    lines = [
        f"⚖ quota-balancer  [{selected_name.upper()}]" + ("" if applied else "  (not written)")
    ]
    for provider in analysis.get("providers") or []:
        bits = []
        for pool in provider["pools"]:
            reset = _reset_label(pool)
            window = pool.get("window") or ""
            label = pool["label"] + (f" {window}" if window else "")
            bits.append(f"{label} {_fmt_amount(pool)}" + (f" · {reset}" if reset else ""))
        if bits:
            lines.append(f"{provider['id']}  " + " · ".join(bits))
    roles = profile.get("modelRoles") or {}
    lines.append(f"default {roles.get('default') or '—'}")
    lines.append(f"slow {roles.get('slow') or '—'} · plan {roles.get('plan') or '—'}")
    if profile.get("claude_source"):
        lines.append(f"Claude: {profile['claude_source']}")
    lines.append("/quota hides this panel · /rebalance refreshes")
    return lines


def generate_profiles(analysis, catalog):
    providers = list(analysis.get("providers") or [])
    known = _by_provider(analysis)
    # Ignore catalog providers that did not return a usage report.
    catalog = [model for model in catalog if model["provider"] in known]
    explicit = explicit_families(analysis)

    grok = _pick_grok(catalog, providers, explicit)
    composer = _pick_composer(catalog, providers, explicit, fast=False)
    composer_fast = _pick_composer(catalog, providers, explicit, fast=True)
    flash = _pick_flash(catalog, providers, explicit)
    claude = _pick_claude(catalog, providers, explicit)
    # A frontier 429 should spend a different provider's Claude pool before the
    # same provider's metered bucket.
    fallback_claude = claude
    if grok and claude and claude["provider"]["id"] == grok["provider"]["id"]:
        for candidate in _claude_candidates(catalog, providers, explicit):
            if candidate["provider"]["id"] != grok["provider"]["id"] and candidate["available"]:
                fallback_claude = candidate
                break

    grok_model = grok["model"] if grok else None
    grok_prev = grok["previous"] if grok else None
    grok_med = selector(grok_model, "medium")
    grok_high = selector(grok_model, "high")
    grok_xhigh = selector(grok_model, "xhigh")
    grok_prev_base = selector(grok_prev)
    grok_prev_high = selector(grok_prev, "high")
    grok_prev_xhigh = selector(grok_prev, "xhigh")
    composer_sel = selector(composer["model"]) if composer else None
    composer_fast_sel = selector(composer_fast["model"]) if composer_fast else None
    gem = flash["model"] if flash else None
    gem_sel = selector(gem)
    gem_high = selector(gem, "high")

    claude_ok = bool(claude and claude["available"])
    if claude_ok:
        opus = claude["opus"]
        sonnet = claude["sonnet"]
        opus_high = selector(opus, "high")
        sonnet_high = selector(sonnet, "high")
        opus_bare = selector(opus)
        sonnet_bare = selector(sonnet)
        opus_prev_bare = selector(claude["opus_prev"])
        sonnet_prev_high = selector(claude["sonnet_prev"], "high")
        sonnet_prev_bare = selector(claude["sonnet_prev"])
        if claude["named"]:
            plan_model = opus_high or sonnet_high or grok_high
            slow_model = opus_high or grok_high
        else:
            plan_model = sonnet_high or opus_high or grok_high
            slow_model = opus_high or grok_high
        review_model = sonnet_high or plan_model
        security_model = opus_high or grok_xhigh
        claude_source = (
            f"{claude['provider']['id']} ({selector(sonnet) or '-'} / {selector(opus) or '-'})"
        )
    else:
        opus_high = sonnet_high = opus_bare = sonnet_bare = None
        opus_prev_bare = sonnet_prev_high = sonnet_prev_bare = None
        plan_model = slow_model = grok_high
        review_model = grok_high
        security_model = grok_xhigh
        claude_source = "Exhausted (falling back to the frontier model loaded in the catalog)"

    fb = fallback_claude if fallback_claude and fallback_claude["available"] else None
    fb_opus_high = selector(fb["opus"], "high") if fb else None
    fb_sonnet_high = selector(fb["sonnet"], "high") if fb else None
    fb_opus_bare = selector(fb["opus"]) if fb else None
    fb_sonnet_bare = selector(fb["sonnet"]) if fb else None

    fast_routine = gem_sel or composer_fast_sel or grok_med
    frontier = grok_high or slow_model or plan_model or fast_routine

    fallback_chains = {
        "default": chain(grok_med, grok_prev_base, gem_sel),
        "slow": chain(grok_high, grok_prev_high, composer_sel, gem_sel),
        "smol": chain(composer_fast_sel, gem_sel),
        "plan": chain(grok_high, grok_prev_high, composer_sel, gem_sel),
        grok_med: chain(grok_prev_base, composer_fast_sel, gem_sel),
        grok_high: chain(grok_prev_high, composer_sel, gem_sel),
        grok_xhigh: chain(grok_prev_xhigh, grok_high, gem_sel),
        grok_prev_base: chain(composer_sel, gem_sel),
        grok_prev_high: chain(composer_sel, gem_sel),
        grok_prev_xhigh: chain(grok_high, gem_sel),
        opus_high: chain(grok_xhigh, grok_high, gem_sel),
        opus_bare: chain(grok_xhigh, grok_high, gem_sel),
        sonnet_high: chain(grok_high, gem_sel),
        sonnet_bare: chain(grok_high, gem_sel),
        sonnet_prev_high: chain(grok_high, gem_sel),
        sonnet_prev_bare: chain(grok_high, gem_sel),
        opus_prev_bare: chain(grok_xhigh, gem_sel),
        fb_opus_high: chain(grok_xhigh, grok_high, gem_sel),
        fb_opus_bare: chain(grok_xhigh, grok_high, gem_sel),
        fb_sonnet_high: chain(grok_high, gem_sel),
        fb_sonnet_bare: chain(grok_high, gem_sel),
        "reviewer": chain(grok_high, gem_sel),
        "security-reviewer": chain(grok_xhigh, gem_sel),
        # A named-pool model must not fall through to the same id on a metered
        # pool (that spends a different provider's paid bucket).
        gem_sel: chain(grok_med, composer_fast_sel),
        gem_high: chain(grok_high, composer_sel),
    }
    for provider in providers:
        if any(pool["kind"] == "named" for pool in provider["pools"]):
            fallback_chains[f"{provider['id']}/*"] = chain(grok_med, composer_fast_sel)
    fallback_chains = {key: value for key, value in fallback_chains.items() if key and value}

    grok_fallbacks = {
        **fallback_chains,
        "slow": chain(fb_opus_high, grok_prev_high, composer_sel, gem_sel),
        "plan": chain(fb_opus_high, grok_prev_high, composer_sel, gem_sel),
        "reviewer": chain(fb_sonnet_high, gem_sel),
        "security-reviewer": chain(fb_opus_high, gem_sel),
    }

    frontier_name = grok_model["id"] if grok_model else "the included frontier model"
    fast_name = gem["id"] if gem else "the named fast model"

    grok_profile = {
        "name": "grok-power",
        "description": f"Prefer {frontier_name} on the included pool. Named fast models stay in the fallback chain.",
        "claude_source": claude_source,
        "claude_active": claude_ok,
        "modelRoles": {
            "default": grok_med or fast_routine,
            "smol": composer_fast_sel or fast_routine,
            "slow": grok_high or frontier,
            "plan": grok_high or frontier,
            "advisor": composer_fast_sel or fast_routine,
            "commit": composer_fast_sel or fast_routine,
            "grok": grok_med or fast_routine,
        },
        "task": {
            "agentModelOverrides": {
                "scout": composer_fast_sel or fast_routine,
                "sonic": composer_fast_sel or fast_routine,
                "reviewer": grok_high or frontier,
                "security-reviewer": grok_xhigh or frontier,
                "task": grok_med or fast_routine,
            }
        },
        "fallbackChains": grok_fallbacks,
    }

    balanced_profile = {
        "name": "balanced",
        "description": f"Routine work on {fast_name}, frontier work on the included pool, Claude when a Claude pool is open ({claude_source}).",
        "claude_source": claude_source,
        "claude_active": claude_ok,
        "modelRoles": {
            "default": fast_routine,
            "smol": fast_routine,
            "slow": grok_high or frontier,
            "plan": plan_model or frontier,
            "advisor": fast_routine,
            "commit": fast_routine,
            "grok": grok_med or fast_routine,
        },
        "task": {
            "agentModelOverrides": {
                "scout": fast_routine,
                "sonic": composer_fast_sel or fast_routine,
                "reviewer": review_model or frontier,
                "security-reviewer": security_model or frontier,
                "task": fast_routine,
            }
        },
        "fallbackChains": fallback_chains,
    }

    conserve_profile = {
        "name": "conserve-cursor",
        "description": f"Reserve the included pool. Routine roles use {fast_name}.",
        "claude_source": claude_source,
        "claude_active": claude_ok,
        "modelRoles": {
            "default": fast_routine,
            "smol": fast_routine,
            "slow": fast_routine,
            "plan": fast_routine,
            "advisor": fast_routine,
            "commit": fast_routine,
            "grok": grok_med or fast_routine,
        },
        "task": {
            "agentModelOverrides": {
                "scout": fast_routine,
                "sonic": fast_routine,
                "reviewer": (review_model or fast_routine) if claude_ok else fast_routine,
                "security-reviewer": (security_model or fast_routine) if claude_ok else fast_routine,
                "task": fast_routine,
            }
        },
        "fallbackChains": fallback_chains,
    }

    claude_first_profile = {
        "name": "claude-first",
        "description": f"Prefer Claude ({claude_source}) for slow, plan, and reviews; other roles use the named fast pool.",
        "claude_source": claude_source,
        "claude_active": claude_ok,
        "modelRoles": {
            "default": fast_routine,
            "smol": fast_routine,
            "slow": slow_model or frontier,
            "plan": plan_model or frontier,
            "advisor": fast_routine,
            "commit": fast_routine,
            "grok": grok_med or fast_routine,
        },
        "task": {
            "agentModelOverrides": {
                "scout": fast_routine,
                "sonic": composer_fast_sel or fast_routine,
                "reviewer": review_model or frontier,
                "security-reviewer": security_model or frontier,
                "task": fast_routine,
            }
        },
        "fallbackChains": fallback_chains,
    }

    frontier_pressure = grok["pressure"] if grok else pool_pressure([
        pool
        for provider in providers
        for pool in provider["pools"]
        if pool["kind"] in ("included", "general")
    ])
    if grok and frontier_pressure < 0.80:
        recommended = "grok-power"
    elif claude_ok:
        recommended = "claude-first"
    elif frontier_pressure < _PERCENT_FULL:
        recommended = "balanced"
    else:
        recommended = "conserve-cursor"

    return {
        "grok-power": grok_profile,
        "claude-first": claude_first_profile,
        "balanced": balanced_profile,
        "conserve-cursor": conserve_profile,
        "recommended": recommended,
    }


def print_dashboard(analysis, profiles):
    rec = profiles["recommended"]
    profile = profiles[rec]
    print("=" * 65)
    print("                OMP QUOTA & MODEL BALANCER")
    print("=" * 65)
    for provider in analysis["providers"]:
        print(f"\n[{provider['id']}]")
        if not provider["pools"]:
            print("  ● no limits reported")
            continue
        for pool in provider["pools"]:
            reset = _reset_label(pool)
            window = pool.get("window") or "window"
            extra = f" · resets in {reset}" if reset else ""
            print(f"  ● {pool['label']} ({window}, {pool['kind']}) : {_fmt_amount(pool)}{extra}")

    print("\n" + "-" * 65)
    print(f"  >>> AUTOMATIC RECOMMENDATION: [{rec.upper()}] <<<")
    print(f"  {profile['description']}")
    print(f"  * Active Claude source: {profile.get('claude_source', 'Unknown')}")
    print("-" * 65)
    print("\nRecommended profile assignment:")
    for role, model in profile["modelRoles"].items():
        print(f"  - {role:<8}: {model}")
    print("\nSubagents (agentModelOverrides):")
    for name, model in profile["task"]["agentModelOverrides"].items():
        print(f"  - {name:<18}: {model}")
    print("\nAutomatic fallback chains (retry.fallbackChains):")
    for target, hops in profile["fallbackChains"].items():
        print(f"  - {target:<40} -> {' -> '.join(hops)}")


def apply_profile(profile_data, quiet=False):
    def say(message):
        if not quiet:
            print(message)

    try:
        import yaml
    except ImportError:
        print("PyYAML is required to write config. Install it with: pip install pyyaml", file=sys.stderr)
        return False

    path = config_path()
    if not path.exists():
        print(f"Config file not found at {path}", file=sys.stderr)
        return False

    with open(path, "r") as handle:
        config = yaml.safe_load(handle) or {}

    roles = {key: value for key, value in profile_data["modelRoles"].items() if value}
    overrides = {key: value for key, value in profile_data["task"]["agentModelOverrides"].items() if value}
    if "default" not in roles:
        print("Catalog is missing a default model; left the config unchanged.", file=sys.stderr)
        return False

    # Keep role keys this profile does not manage.
    merged_roles = dict(config.get("modelRoles") or {})
    merged_roles.update(roles)
    config["modelRoles"] = merged_roles
    if "task" not in config or not isinstance(config["task"], dict):
        config["task"] = {}
    config["task"]["agentModelOverrides"] = overrides
    if "retry" not in config or not isinstance(config["retry"], dict):
        config["retry"] = {}
    config["retry"]["fallbackChains"] = profile_data["fallbackChains"]

    with open(path, "w") as handle:
        yaml.dump(config, handle, sort_keys=False)

    say(f"\n[OK] Wrote profile [{profile_data['name']}] to {path}.")
    say("Start a new OMP session to reload modelRoles and retry.fallbackChains.")
    return True


def main():
    import argparse
    parser = argparse.ArgumentParser(description="OMP Dynamic Quota Balancer")
    parser.add_argument("--apply", action="store_true", help="Write the selected profile into the active OMP config.yml")
    parser.add_argument(
        "--profile",
        choices=["grok-power", "claude-first", "balanced", "conserve-cursor", "conserve-included", "auto"],
        default="auto",
        help="Profile to inspect or apply. conserve-included is an alias of conserve-cursor.",
    )
    parser.add_argument("--json", action="store_true", help="Output analysis in JSON format")
    args = parser.parse_args()
    if args.profile == "conserve-included":
        args.profile = "conserve-cursor"

    usage = get_usage()
    if not usage:
        print("Failed to fetch usage from OMP.", file=sys.stderr)
        sys.exit(1)

    try:
        catalog = load_catalog()
    except Exception as e:
        print(f"Error loading model catalog: {e}", file=sys.stderr)
        sys.exit(1)
    if not catalog:
        print("Model catalog is empty; left the config unchanged.", file=sys.stderr)
        sys.exit(1)

    analysis = analyze_quotas(usage)
    if not analysis or not analysis.get("providers"):
        print("No provider usage reports.", file=sys.stderr)
        sys.exit(1)

    profiles = generate_profiles(analysis, catalog)
    selected_name = profiles["recommended"] if args.profile == "auto" else args.profile
    selected = profiles[selected_name]

    applied = False
    if args.apply:
        applied = apply_profile(selected, quiet=args.json)

    if args.json:
        print(json.dumps({
            "analysis": analysis,
            "selected": selected_name,
            "applied": applied,
            "level": pressure_level(analysis),
            "summary": summary_line(analysis),
            "panel": panel_lines(analysis, selected_name, selected, applied),
            "profile": {
                "name": selected["name"],
                "description": selected["description"],
                "claude_source": selected.get("claude_source"),
                "modelRoles": selected["modelRoles"],
                "agentModelOverrides": selected["task"]["agentModelOverrides"],
            },
        }))
        return

    print_dashboard(analysis, profiles)
    if not args.apply:
        print("\nTip: run `--apply` or `--profile", selected_name, "--apply` to write these changes.")
        print("The config path is `omp config path` (override with PI_CODING_AGENT_DIR).")


if __name__ == "__main__":
    main()
