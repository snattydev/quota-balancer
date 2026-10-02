"""Pool coverage follows usage reports, not provider names."""

import sys
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
