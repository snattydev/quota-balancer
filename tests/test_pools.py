"""Pool coverage follows usage reports, not provider names."""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import balance  # noqa: E402


def _limit(limit_id, label, window, fraction, unit="percent", used=None, limit=None, shared=None):
    amount = {"usedFraction": fraction, "unit": unit}
    if used is not None:
        amount["used"] = used
    if limit is not None:
        amount["limit"] = limit
    scope = {"windowId": window}
    if shared:
        scope["shared"] = True
        scope["sharedGroup"] = shared
    return {
        "id": limit_id,
        "label": label,
        "scope": scope,
        "window": {"id": window, "label": window, "durationMs": balance._WINDOW_MS.get(window)},
        "amount": amount,
    }


def _usage(included=0.10, other=0.0, gemini=0.0, claude=0.0):
    return {
        "reports": [
            {
                "provider": "alpha",
                "limits": [
                    _limit("alpha:usd:individual-auto", "Alpha Models", "monthly", included, used=included * 100),
                    _limit("alpha:usd:individual-api", "Other Models", "monthly", other, unit="usd", used=other * 20, limit=20),
                ],
            },
            {
                "provider": "beta",
                "limits": [
                    _limit("beta:google:default:gemini-5h", "Gemini", "5h", gemini),
                    _limit("beta:google:default:gemini-weekly", "Gemini", "weekly", gemini),
                    _limit("beta:anthropic:default:3p-5h", "Claude & GPT (shared)", "5h", claude, shared="3p-5h:5h"),
                    _limit("beta:openai:default:3p-5h", "Claude & GPT (shared)", "5h", claude, shared="3p-5h:5h"),
                    _limit("beta:anthropic:default:3p-weekly", "Claude & GPT (shared)", "weekly", claude, shared="3p-weekly:weekly"),
                ],
            },
            {
                "provider": "acme",
                "limits": [
                    _limit("acme:requests", "Requests", "monthly", 0.10),
                ],
            },
        ]
    }


def _model(provider, model_id, efforts=None):
    return balance._normalize_model({
        "provider": provider,
        "id": model_id,
        "thinking": efforts or ["low", "medium", "high", "xhigh"],
    })


CATALOG = [
    _model("alpha", "grok-4.7"),
    _model("alpha", "grok-4.6"),
    _model("alpha", "composer-2.5"),
    _model("alpha", "composer-2.5-fast"),
    _model("alpha", "claude-opus-4-8"),
    _model("alpha", "claude-sonnet-5"),
    _model("alpha", "gemini-3.8-flash"),
    _model("beta", "gemini-3.8-flash"),
    _model("beta", "gemini-3.1-flash"),
    _model("beta", "claude-opus-4-6"),
    _model("beta", "claude-sonnet-4-6"),
    _model("acme", "mini-model"),
]


class PoolCoverageTest(unittest.TestCase):
    def setUp(self):
        self.analysis = balance.analyze_quotas(_usage())
        self.explicit = balance.explicit_families(self.analysis)
        self.providers = {item["id"]: item for item in self.analysis["providers"]}

    def test_discovers_every_reported_provider(self):
        self.assertEqual(set(self.providers), {"alpha", "beta", "acme"})

    def test_kinds_come_from_limit_text(self):
        alpha = {pool["label"]: pool["kind"] for pool in self.providers["alpha"]["pools"]}
        self.assertEqual(alpha["Alpha Models"], "included")
        self.assertEqual(alpha["Other Models"], "metered")
        beta_kinds = {pool["label"]: pool["kind"] for pool in self.providers["beta"]["pools"]}
        self.assertEqual(beta_kinds["Gemini"], "named")
        self.assertEqual(beta_kinds["Claude & GPT (shared)"], "named")
        self.assertEqual(self.providers["acme"]["pools"][0]["kind"], "general")

    def test_shared_windows_collapse(self):
        claude_pools = [
            pool for pool in self.providers["beta"]["pools"]
            if "claude" in pool["families"]
        ]
        self.assertEqual(len(claude_pools), 2)
        self.assertIn("gpt", claude_pools[0]["families"])

    def test_models_follow_the_pool_that_names_their_family(self):
        grok = next(model for model in CATALOG if model["id"] == "grok-4.7")
        included = balance.covering_pools(self.providers["alpha"], grok, self.explicit)
        self.assertEqual([pool["kind"] for pool in included], ["included"])

        claude = next(model for model in CATALOG if model["provider"] == "alpha" and "opus" in model["id"])
        metered = balance.covering_pools(self.providers["alpha"], claude, self.explicit)
        self.assertEqual([pool["kind"] for pool in metered], ["metered"])

        named = next(model for model in CATALOG if model["provider"] == "beta" and model["id"] == "gemini-3.8-flash")
        gemini = balance.covering_pools(self.providers["beta"], named, self.explicit)
        self.assertTrue(all(pool["kind"] == "named" for pool in gemini))

        twin = next(model for model in CATALOG if model["provider"] == "alpha" and model["id"] == "gemini-3.8-flash")
        twin_pools = balance.covering_pools(self.providers["alpha"], twin, self.explicit)
        self.assertEqual([pool["kind"] for pool in twin_pools], ["metered"])

        mini = next(model for model in CATALOG if model["provider"] == "acme")
        general = balance.covering_pools(self.providers["acme"], mini, self.explicit)
        self.assertEqual(general[0]["kind"], "general")

    def test_low_included_pool_prefers_frontier_profile(self):
        profiles = balance.generate_profiles(self.analysis, CATALOG)
        self.assertEqual(profiles["recommended"], "grok-power")
        self.assertEqual(profiles["grok-power"]["modelRoles"]["default"], "alpha/grok-4.7:medium")
        self.assertEqual(profiles["balanced"]["modelRoles"]["default"], "beta/gemini-3.8-flash")
        hops = profiles["balanced"]["fallbackChains"]["beta/gemini-3.8-flash"]
        self.assertNotIn("alpha/gemini-3.8-flash", hops)
        self.assertEqual(profiles["grok-power"]["fallbackChains"]["slow"][0], "beta/claude-opus-4-6:high")
        self.assertIn("beta/*", profiles["balanced"]["fallbackChains"])

    def test_open_claude_pool_after_the_included_pool_fills(self):
        analysis = balance.analyze_quotas(_usage(included=0.85, other=0.10, claude=0.0))
        profiles = balance.generate_profiles(analysis, CATALOG)
        self.assertEqual(profiles["recommended"], "claude-first")
        # beta's Claude window is emptier than alpha's metered pool, so it wins.
        self.assertEqual(profiles["claude-first"]["modelRoles"]["plan"], "beta/claude-opus-4-6:high")
        self.assertTrue(profiles["claude-first"]["claude_source"].startswith("beta "))

    def test_full_pools_reserve_the_included_bucket(self):
        analysis = balance.analyze_quotas(_usage(included=0.96, other=0.99, gemini=0.10, claude=0.96))
        profiles = balance.generate_profiles(analysis, CATALOG)
        self.assertEqual(profiles["recommended"], "conserve-cursor")
        self.assertEqual(profiles["conserve-cursor"]["modelRoles"]["default"], "beta/gemini-3.8-flash")

    def test_source_does_not_name_providers(self):
        text = Path(balance.__file__).read_text()
        self.assertNotIn("google-antigravity", text)
        self.assertNotIn("Cursor Models", text)
        for report in _usage()["reports"]:
            self.assertNotIn(f'"{report["provider"]}"', text)


class FractionTest(unittest.TestCase):
    def test_percent_used_without_fraction(self):
        limit = {"amount": {"used": 9.0, "unit": "percent"}}
        self.assertAlmostEqual(balance._used_fraction(limit), 0.09)

    def test_explicit_fraction_wins(self):
        limit = {"amount": {"used": 9.0, "usedFraction": 0.25, "unit": "percent"}}
        self.assertAlmostEqual(balance._used_fraction(limit), 0.25)


class SingleProviderAndFallbackTest(unittest.TestCase):
    def test_single_provider_routes_metered_families_without_second_provider(self):
        usage = {
            "reports": [
                {
                    "provider": "alpha",
                    "limits": [
                        _limit("alpha:included", "Alpha Models", "monthly", 0.85),
                        _limit("alpha:other", "Other Models", "monthly", 1.0, unit="usd", used=20, limit=20),
                    ],
                }
            ]
        }
        analysis = balance.analyze_quotas(usage)
        explicit = balance.explicit_families(analysis)
        self.assertEqual(explicit, set())
        alpha = analysis["providers"][0]

        opus = next(m for m in CATALOG if m["provider"] == "alpha" and "opus" in m["id"])
        gem = next(m for m in CATALOG if m["provider"] == "alpha" and "gemini" in m["id"])
        grok = next(m for m in CATALOG if m["provider"] == "alpha" and m["id"] == "grok-4.7")
        self.assertEqual([p["kind"] for p in balance.covering_pools(alpha, opus, explicit)], ["metered"])
        self.assertEqual([p["kind"] for p in balance.covering_pools(alpha, gem, explicit)], ["metered"])
        self.assertEqual([p["kind"] for p in balance.covering_pools(alpha, grok, explicit)], ["included"])

        profiles = balance.generate_profiles(analysis, CATALOG)
        self.assertFalse(profiles["claude-first"]["claude_active"])
        self.assertEqual(profiles["recommended"], "balanced")

    def test_provider_own_family_stays_on_included_pool(self):
        usage = {
            "reports": [
                {
                    "provider": "anthropic",
                    "limits": [
                        _limit("anth:included", "Included Models", "monthly", 0.20),
                        _limit("anth:other", "Other Models", "monthly", 0.99, unit="usd", used=19.8, limit=20),
                    ],
                }
            ]
        }
        analysis = balance.analyze_quotas(usage)
        provider = analysis["providers"][0]
        opus = _model("anthropic", "claude-opus-4-6")
        gem = _model("anthropic", "gemini-3.8-flash")
        self.assertEqual([p["kind"] for p in balance.covering_pools(provider, opus, set())], ["included"])
        self.assertEqual([p["kind"] for p in balance.covering_pools(provider, gem, set())], ["metered"])

    def test_exhausted_named_flash_yields_to_open_pool(self):
        usage = {
            "reports": [
                {
                    "provider": "beta",
                    "limits": [_limit("beta:gemini", "Gemini", "5h", 1.0)],
                },
                {
                    "provider": "gamma",
                    "limits": [_limit("gamma:req", "Requests", "monthly", 0.10)],
                },
            ]
        }
        catalog = [
            _model("beta", "gemini-3.8-flash"),
            _model("gamma", "gemini-3.8-flash"),
        ]
        analysis = balance.analyze_quotas(usage)
        flash = balance._pick_flash(catalog, analysis["providers"], balance.explicit_families(analysis))
        self.assertEqual(flash["provider"]["id"], "gamma")

    def test_available_usd_grok_beats_exhausted_percent_grok(self):
        usage = {
            "reports": [
                {
                    "provider": "pct",
                    "limits": [_limit("pct:inc", "Pct Models", "monthly", 0.91, unit="percent")],
                },
                {
                    "provider": "usd",
                    "limits": [_limit("usd:inc", "Usd Models", "monthly", 0.92, unit="usd", used=18.4, limit=20)],
                },
            ]
        }
        catalog = [
            _model("pct", "grok-4.7"),
            _model("usd", "grok-4.7"),
        ]
        analysis = balance.analyze_quotas(usage)
        grok = balance._pick_grok(catalog, analysis["providers"], set())
        self.assertEqual(grok["provider"]["id"], "usd")
        self.assertTrue(grok["available"])

    def test_generic_provider_without_grok_or_claude_populates_roles(self):
        usage = {
            "reports": [
                {
                    "provider": "delta",
                    "limits": [_limit("delta:req", "Requests", "monthly", 0.25)],
                }
            ]
        }
        catalog = [
            _model("delta", "gpt-5"),
            _model("delta", "gpt-5-mini"),
        ]
        analysis = balance.analyze_quotas(usage)
        profiles = balance.generate_profiles(analysis, catalog)
        rec = profiles[profiles["recommended"]]
        self.assertEqual(rec["modelRoles"]["default"], "delta/gpt-5-mini")
        self.assertEqual(rec["modelRoles"]["slow"], "delta/gpt-5:high")

    def test_revision_and_claude_matching(self):
        dated = _model("p", "claude-sonnet-4-20250514")
        older_dated = _model("p", "claude-sonnet-4-20250219")
        minor = _model("p", "claude-sonnet-4-6")
        legacy = _model("p", "claude-3-5-sonnet-20241022")
        self.assertGreater(balance._revision(minor), balance._revision(dated))
        self.assertGreater(balance._revision(dated), balance._revision(older_dated))
        self.assertGreater(balance._revision(older_dated), balance._revision(legacy))
        self.assertEqual(balance._claude([legacy], "p", "sonnet")["id"], "claude-3-5-sonnet-20241022")
        self.assertEqual(balance._claude([dated, minor, legacy], "p", "sonnet")["id"], "claude-sonnet-4-6")

    def test_role_fallbacks_exclude_primary_and_conserve_avoids_included_first(self):
        analysis = balance.analyze_quotas(_usage(included=0.95, other=0.10, gemini=0.10, claude=0.10))
        profiles = balance.generate_profiles(analysis, CATALOG)
        for name in ("grok-power", "claude-first", "balanced", "conserve-cursor"):
            profile = profiles[name]
            primary = {**profile["modelRoles"], **profile["task"]["agentModelOverrides"]}
            for role, assigned in primary.items():
                hops = profile["fallbackChains"].get(role, [])
                self.assertNotIn(assigned, hops, f"{name}.{role} fell back onto itself")
        conserve_slow = profiles["conserve-cursor"]["fallbackChains"]["slow"]
        self.assertEqual(conserve_slow[0], "beta/claude-opus-4-6:high")

    def test_selector_validates_efforts_and_avoids_duplicate_suffix(self):
        m = _model("p", "model-x", efforts=["low", "high"])
        self.assertEqual(balance.selector(m, "high"), "p/model-x:high")
        self.assertEqual(balance.selector(m, "xhigh"), "p/model-x")
        suffixed = _model("p", "model-x-high", efforts=["high"])
        self.assertEqual(balance.selector(suffixed, "high"), "p/model-x-high")

    def test_apply_profile_preserves_unmanaged_roles_and_is_atomic(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.yml"
            cfg.write_text("modelRoles:\n  custom: keep/me\n  default: old/model\n")
            os.chmod(cfg, 0o600)
            analysis = balance.analyze_quotas(_usage())
            profile = balance.generate_profiles(analysis, CATALOG)["grok-power"]
            with mock.patch.dict(os.environ, {"PI_CODING_AGENT_DIR": tmp}):
                ok = balance.apply_profile(profile, quiet=True)
            self.assertTrue(ok)
            written = yaml.safe_load(cfg.read_text())
            self.assertEqual(written["modelRoles"]["custom"], "keep/me")
            self.assertEqual(written["modelRoles"]["default"], "alpha/grok-4.7:medium")
            self.assertEqual(stat.S_IMODE(cfg.stat().st_mode), 0o600)
            leftovers = [p.name for p in Path(tmp).iterdir() if p.name.endswith(".tmp")]
            self.assertEqual(leftovers, [])

    def test_apply_profile_returns_false_when_missing_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.yml"
            cfg.write_text("modelRoles:\n  default: keep/me\n")
            bad_profile = {
                "name": "broken",
                "modelRoles": {"default": None},
                "task": {"agentModelOverrides": {}},
                "fallbackChains": {},
            }
            with mock.patch.dict(os.environ, {"PI_CODING_AGENT_DIR": tmp}):
                ok = balance.apply_profile(bad_profile, quiet=True)
            self.assertFalse(ok)
            self.assertIn("keep/me", cfg.read_text())


if __name__ == "__main__":
    unittest.main()
