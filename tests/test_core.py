"""Offline tests: identity gate, names, refresh diff, input parsing and envelope contract. No network access."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from signalpost.agent import assemble, family_states
from signalpost.cli import read_inputs
from signalpost.evidence import Ledger
from signalpost.names import core_name, domain_candidates, full_name_in_text, name_in_text, orgnrs_in, phone_digits, valid_orgnr
from signalpost.net import Snapshot, check_url, UrlPolicyError
from signalpost.page import Page
from signalpost.refresh import diff
from signalpost.synthesis import build_synthesis, derived_metrics, key_points
from signalpost.website import SiteVisit, _proof, _extract, _publish_registry_declared

ORG = "810034882"


def visit(url: str, html: str) -> SiteVisit:
    snap = Snapshot(url, url, 200, "2026-10-20T00:00:00Z", content_sha256="x" * 64, content_type="text/html", text=html)
    return SiteVisit(snap, Page(url, html))


def ctx(**kw) -> dict:
    base = {"organisation_number": ORG, "legal_name": "SANDNES ELEKTRISKE AS", "former_names": [], "phone": "51 68 57 00",
            "email": "post@sandneselektriske.no", "address": {"adresse": ["Strandgata 21"], "postnummer": "4307", "poststed": "SANDNES"},
            "subunits": [], "roles": [{"name": "Morten Aarrestad", "is_person": True, "role_code": "DAGL", "role": "Daglig leder"}],
            "legal_form": "AS", "industry_code": "43.210"}
    base.update(kw)
    return base


class NamesTest(unittest.TestCase):
    def test_orgnr_checksum(self):
        self.assertTrue(valid_orgnr("923609016"))
        self.assertFalse(valid_orgnr("923609017"))

    def test_core_name_and_candidates(self):
        self.assertEqual(core_name("SANDNES ELEKTRISKE AS"), "SANDNES ELEKTRISKE")
        self.assertIn("sandneselektriske.no", domain_candidates("SANDNES ELEKTRISKE AS"))
        self.assertIn("helgoyfrukt.no", domain_candidates("HELGØYFRUKT AS"))

    def test_name_in_text_is_whole_word(self):
        self.assertTrue(name_in_text("SANDNES ELEKTRISKE AS", "Velkommen til Sandnes Elektriske!"))
        self.assertFalse(name_in_text("SB TRANSPORT AS", "ASB Transporter"))

    def test_orgnrs_need_context(self):
        self.assertEqual(orgnrs_in("Org.nr: 923 609 016"), {"923609016"})
        self.assertEqual(orgnrs_in("Telefon 923 609 016"), set())

    def test_phone_digits(self):
        self.assertEqual(phone_digits("+47 51 68 57 00"), "51685700")
        self.assertIsNone(phone_digits("123"))


class UrlPolicyTest(unittest.TestCase):
    def test_rejects_private_and_odd_urls(self):
        for url in ("file:///etc/passwd", "http://127.0.0.1/", "http://localhost/", "http://user:pw@example.com/", "http://10.0.0.1:8080/"):
            with self.assertRaises(UrlPolicyError):
                check_url(url)


class UrlEncodingTest(unittest.TestCase):
    def test_spaces_in_path_are_encoded(self):
        self.assertTrue(check_url("https://www.brreg.no/Om oss").endswith("/Om%20oss"))


class IdentityGateTest(unittest.TestCase):
    def test_orgnr_on_page_is_exact(self):
        v = visit("https://sandneselektriske.no/", "<html lang='no'><body>Sandnes Elektriske AS · Org.nr. 810 034 882</body></html>")
        proof = _proof(ctx(), v, "name_guess", "sandneselektriske.no")
        self.assertEqual(proof["level"], "organisation_number_on_page")
        self.assertFalse(proof.get("reject"))

    def test_phone_without_name_on_unrelated_domain_is_rejected(self):
        html = "<html lang='no'><title>Regnskapsbyrå Vest</title><body>Ring oss 51 68 57 00. Regnskapsbyrå Vest AS</body></html>"
        v = visit("https://regnskapvest.no/", html)
        proof = _proof(ctx(), v, "registry_email_domain", "regnskapvest.no")
        self.assertTrue(proof is None or proof.get("reject"))

    def test_page_naming_other_entity_is_rejected(self):
        html = "<html lang='no'><title>HJEM | Stein Design As</title><body>post@steindesign.no Stein Design As</body></html>"
        v = visit("https://steindesign.no/", html)
        c = ctx(legal_name="SD HOLDING AS", email="post@steindesign.no", phone=None, industry_code="64.200")
        proof = _proof(c, v, "registry_email_domain", "steindesign.no")
        self.assertTrue(proof.get("reject"))

    def test_managed_housing_cooperative_needs_own_domain(self):
        html = "<html lang='no'><body>Lyngveien borettslag forvaltes av oss. Telefon 22 22 22 22</body></html>"
        v = visit("https://usbl.no/", html)
        c = ctx(legal_name="LYNGVEIEN BORETTSLAG", legal_form="BRL", phone="22 22 22 22", email=None)
        self.assertTrue(_proof(c, v, "registry_website", "usbl.no").get("reject"))

    def test_foreign_group_site_is_rejected(self):
        html = "<html lang='de'><body>Smith &amp; Nephew AS Deutschland kontakt@smith-nephew.com</body></html>"
        v = visit("https://www.smith-nephew.com/de-de", html)
        c = ctx(legal_name="SMITH & NEPHEW AS", email="kontakt@smith-nephew.com", phone=None)
        self.assertTrue(_proof(c, v, "registry_email_domain", "smith-nephew.com").get("reject"))

    def test_name_and_leader(self):
        html = "<html lang='no'><body>Sandnes Elektriske – daglig leder Morten Aarrestad</body></html>"
        v = visit("https://sandneselektriske.no/", html)
        proof = _proof(ctx(phone=None, email=None), v, "name_guess", "sandneselektriske.no")
        self.assertEqual(proof["level"], "legal_name_and_registered_leader_on_page")


    def test_registered_name_with_legal_form_on_name_domain(self):
        html = "<html lang='no'><body>Velkommen! Sandnes Elektriske AS leverer elektrikertjenester.</body></html>"
        v = visit("https://sandneselektriske.no/", html)
        proof = _proof(ctx(phone=None, email=None, roles=[]), v, "name_guess", "sandneselektriske.no")
        self.assertEqual(proof["level"], "registered_name_with_legal_form_on_name_domain")
        # the core name alone, or a longer sister name, is not enough
        v2 = visit("https://sandneselektriske.no/", "<html lang='no'><body>Sandnes Elektriske Bygg AS</body></html>")
        self.assertTrue(_proof(ctx(phone=None, email=None, roles=[]), v2, "name_guess", "sandneselektriske.no").get("reject"))
        self.assertIsNone(full_name_in_text("SANDNES ELEKTRISKE AS", "Sandnes Elektriske ASA"))

    def test_registered_address_needs_name_or_name_domain(self):
        # parent company's site reached through the registry e-mail domain: address shared, name absent -> no proof
        html = "<html lang='no'><body>Molde og Romsdal Havn IKS Strandgata 21 4307 Sandnes</body></html>"
        v = visit("https://havn.no/", html)
        proof = _proof(ctx(phone=None, email="post@havn.no", roles=[]), v, "registry_email_domain", "havn.no")
        self.assertTrue(proof is None or proof.get("reject"))
        html = "<html lang='no'><body>Elektrikeren i Sandnes – Strandgata 21, 4307 Sandnes</body></html>"
        v = visit("https://sandneselektriske.no/", html)
        proof = _proof(ctx(phone=None, roles=[]), v, "registry_email_domain", "sandneselektriske.no")
        self.assertEqual(proof["level"], "registry_email_domain_with_registered_address")

    def test_registry_declared_website_is_labelled_and_not_crawled(self):
        reg = Snapshot("https://data.brreg.no/x", "https://data.brreg.no/x", 200, "t", content_sha256="y" * 64, content_type="application/json", text="{}")
        page = visit("https://www.kjede.no/", "<html lang='no'><title>Kjeden</title><body>Kjeden AS</body></html>")
        ledger = Ledger(ORG)
        c = ctx(website="www.kjede.no", entity_snapshot=reg,
                registry_site_unproven={"url": "https://www.kjede.no/", "result": "site_names_other_entity", "visit": page})
        self.assertTrue(_publish_registry_declared(c, ledger))
        site = ledger.claims_for("official_website")[0]
        self.assertEqual(site["relation"], "registry_declared")
        self.assertEqual(ledger.field_states["social_profile"]["state"], "not_available")
        # redirected to another company's domain, or a housing cooperative on its manager's site: nothing is published
        for extra in ({"registry_site_unproven": {"url": "https://nordlo.com/", "result": "no_identity_proof", "visit": page}},
                      {"legal_name": "LYNGVEIEN BORETTSLAG", "legal_form": "BRL"}):
            c2 = dict(c, **extra)
            self.assertFalse(_publish_registry_declared(c2, Ledger(ORG)))
        c3 = dict(c, registry_site_unproven={"url": "https://www.kjede.no/", "result": "parked_or_placeholder", "visit": page})
        self.assertFalse(_publish_registry_declared(c3, Ledger(ORG)))


    def test_holding_named_only_by_former_name_is_rejected(self):
        html = "<html lang='no'><body>Velkommen til Sauda Maskinering AS. Daglig leder: Odd-Åge Hustveit</body></html>"
        v = visit("https://saudamaskinering.no/", html)
        c = ctx(legal_name="SAUDA HOLDING AS", former_names=["SAUDA MASKINERING AS"], phone=None, email=None, industry_code="64.200",
                roles=[{"name": "Odd-Åge Hustveit", "is_person": True, "role_code": "DAGL"}])
        self.assertTrue(_proof(c, v, "former_name_guess", "saudamaskinering.no").get("reject"))


class SynthesisTest(unittest.TestCase):
    def test_derived_ratios_cite_claims_and_points_are_supported(self):
        snap = Snapshot("https://data.brreg.no/r", "https://data.brreg.no/r", 200, "t", content_sha256="z" * 64, content_type="application/json", text="{}")
        ledger = Ledger(ORG)
        accounts = []
        for year, rev, op, net, eq, assets in (("2023", 100, 10, 8, 40, 100), ("2024", 110, 12, 9, 45, 100), ("2025", 121, 14, 10, 50, 100)):
            period = f"{year}-01-01/{year}-12-31"
            accounts.append({"period": period, "year": year, "currency": "NOK", "revenue": rev * 1e6, "operating_result": op * 1e6,
                             "net_result": net * 1e6, "equity": eq * 1e6, "total_assets": assets * 1e6})
            for field, value in (("revenue", rev), ("operating_result", op), ("net_result", net), ("equity", eq), ("total_assets", assets)):
                ev = ledger.add_evidence(snap, source_class="official_annual_accounts", method="json_path", span=f"{field}={value}")
                ledger.claim("financials", field, {"amount": value * 1e6, "currency": "NOK", "year": year}, [ev], key=(field, period), reporting_period=period)
        ev = ledger.add_evidence(snap, source_class="official_registry", method="json_path", span="navn")
        ledger.claim("identity", "legal_name", "SANDNES ELEKTRISKE AS", [ev])
        c = ctx(accounts=accounts, roles=[{"name": "Kari Nordmann", "role_code": "DAGL", "is_person": True},
                                          {"name": "Kari Nordmann", "role_code": "LEDE", "is_person": True}])
        metrics = {m["metric"]: m for m in derived_metrics(c, ledger)}
        self.assertAlmostEqual(metrics["operating_margin"]["value"], 14 / 121, places=3)
        self.assertAlmostEqual(metrics["equity_ratio"]["value"], 0.5)
        self.assertAlmostEqual(metrics["revenue_cagr"]["value"], 0.1, places=3)
        for m in metrics.values():
            self.assertTrue(m["claim_ids"] and all(cid in ledger.claims for cid in m["claim_ids"]))
        texts = [p["text"] for p in key_points(c, ledger, list(metrics.values()), "2026-10-21")]
        self.assertIn("Profitable in every filed year shown (2023–2025).", texts)
        self.assertTrue(any(t.startswith("Revenue is growing") for t in texts))
        syn = build_synthesis(c, ledger, [], today="2026-10-21")
        self.assertTrue(syn["headline"]["text"].startswith("SANDNES ELEKTRISKE AS"))
        for item in syn["key_points"] + syn["sentences"]:
            self.assertTrue(all(cid in ledger.claims for cid in item["claim_ids"]))
        env = assemble(ORG, ledger, {"entity": {"navn": "SANDNES ELEKTRISKE AS"}}, run_id="r", started="t", completed="t", terminal="completed",
                       synthesis=syn, changes=[], refresh_meta={}, operations={})
        self.assertTrue(any(u["family"] == "official_website" for u in env["synthesis"]["unknowns"]))
        from signalpost.viewer import company_page, index_page, summary_row
        html = company_page(env)
        self.assertIn("Key points", html)
        self.assertIn("equity / total assets", html)
        self.assertIn("Sandnes Elektriske AS".upper(), index_page([summary_row(env)], {"run_id": "r"}))


class ExtractionTest(unittest.TestCase):
    def test_socials_contacts_and_share_links(self):
        html = """<html lang='no'><head><meta property='og:site_name' content='Sandnes Elektriske'></head><body>
        <a href='https://www.facebook.com/sandneselektriske'>Facebook</a>
        <a href='https://www.facebook.com/sharer.php?u=x'>Del</a>
        <a href='https://www.facebook.com/terms.php'>Vilkår</a>
        <a href='mailto:post@sandneselektriske.no'>post@sandneselektriske.no</a>
        <a href='tel:+4751685700'>51 68 57 00</a>
        <p>Strandgata 21, 4307 Sandnes</p></body></html>"""
        v = visit("https://sandneselektriske.no/", html)
        ledger = Ledger(ORG)
        c = ctx()
        _extract(c, {"domain": "sandneselektriske.no", "home": "https://sandneselektriske.no/"}, [v], {"home": [v]}, ledger)
        fields = {}
        for claim in ledger.claims.values():
            fields.setdefault(claim["field"], []).append(claim["value"])
        self.assertEqual([s["url"] for s in fields["social_profile"]], ["https://www.facebook.com/sandneselektriske"])
        self.assertIn("post@sandneselektriske.no", fields["contact_email"])
        self.assertIn("+47 51 68 57 00", fields["contact_phone"])
        self.assertIn("Strandgata 21, 4307 Sandnes", fields["website_location"])
        self.assertEqual(fields["public_brand"], ["Sandnes Elektriske"])
        for claim in ledger.claims.values():
            self.assertTrue(claim["evidence_ids"])


def _ledger_with(values: dict) -> Ledger:
    ledger = Ledger(ORG)
    snap = Snapshot("https://data.brreg.no/x", "https://data.brreg.no/x", 200, "2026-10-20T00:00:00Z", content_sha256="a" * 64)
    for field, value in values.items():
        ev = ledger.add_evidence(snap, source_class="official_registry", span=f"{field}={value}", method="json_path")
        ledger.claim("identity", field, value, [ev])
    return ledger


class RefreshTest(unittest.TestCase):
    def test_same_snapshot_is_idempotent(self):
        first = _ledger_with({"legal_name": "A AS", "business_address": "Gate 1, 0001 OSLO"})
        changes, _ = diff(first, None, "t1")
        env1 = assemble(ORG, first, {}, run_id="r1", started="t1", completed="t1", terminal="completed", synthesis={},
                        changes=changes, refresh_meta={}, operations={})
        second = _ledger_with({"legal_name": "A AS", "business_address": "Gate 1, 0001 OSLO"})
        changes2, meta = diff(second, env1, "t2")
        self.assertEqual(changes2, [])
        self.assertEqual(meta["unchanged"], 2)
        self.assertTrue(all(c["first_observed"] == "t1" for c in second.claims.values()))

    def test_changed_single_value_and_preserved_history(self):
        first = _ledger_with({"business_address": "Gate 1, 0001 OSLO"})
        env1 = assemble(ORG, first, {}, run_id="r1", started="t1", completed="t1", terminal="completed", synthesis={},
                        changes=diff(first, None, "t1")[0], refresh_meta={}, operations={})
        second = _ledger_with({"business_address": "Gate 2, 0002 OSLO"})
        changes, meta = diff(second, env1, "t2")
        self.assertEqual([c["type"] for c in changes], ["changed_business_address"])
        self.assertEqual(changes[0]["previous_value"], "Gate 1, 0001 OSLO")
        self.assertTrue(meta["history_claims"])
        self.assertTrue(any(e.get("superseded") for e in second.evidence.values()))


class ContractTest(unittest.TestCase):
    def test_states_are_explicit_and_unavailable_claims_have_no_value(self):
        ledger = _ledger_with({"legal_name": "A AS"})
        ledger.mark("official_website", "not_available", "nothing found")
        env = assemble(ORG, ledger, {}, run_id="r", started="t", completed="t", terminal="completed", synthesis={}, changes=[],
                       refresh_meta={}, operations={})
        allowed = {"available", "not_available", "blocked", "not_applicable", "ambiguous", "failed"}
        self.assertIn(env["state"], allowed)
        for claim in env["claims"]:
            self.assertIn(claim["availability"], allowed)
            if claim["availability"] != "available":
                self.assertIsNone(claim["value"])
        self.assertEqual(env["field_families"]["official_website"]["state"], "not_available")
        self.assertEqual(family_states(Ledger(ORG), False, "failed")["leadership"]["state"], "failed")

    def test_input_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / "a.jsonl").write_text('{"organisation_number":"923609016"}\n{"organisation_number":"810034882"}\n')
            (p / "b.json").write_text(json.dumps({"organisation_numbers": ["923 609 016"]}))
            (p / "c.txt").write_text("923609016\n810034882\n")
            self.assertEqual(read_inputs(str(p / "a.jsonl")), ["923609016", "810034882"])
            self.assertEqual(read_inputs(str(p / "b.json")), ["923609016"])
            self.assertEqual(read_inputs(str(p / "c.txt")), ["923609016", "810034882"])


if __name__ == "__main__":
    unittest.main()
