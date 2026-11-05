import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import jd_flags  # noqa: E402


def sponsorship(text):
    return jd_flags.detect(text)["sponsorship"]


def clearance(text):
    return jd_flags.detect(text)["clearance"]


def citizenship(text):
    return jd_flags.detect(text)["us_citizenship_required"]


class SponsorshipTests(unittest.TestCase):
    def test_false_phrasings(self):
        for text in [
            "We do not sponsor H-1B visas.",
            "We do not offer sponsorship for this role.",
            "Visa Sponsorship is not available for this position.",
            "No visa sponsorship is available.",
            "We are unable to sponsor work visas.",
            "At this time, Acme is unable to consider candidates who require current or future visa sponsorship.",
            "Applicants must have authorization to work in the United States without the need for sponsorship now or in the future.",
            "This position is not eligible for employment based sponsorship",
            "We cannot provide immigration sponsorship.",
            "The company will not sponsor an employment visa.",
            "Candidates must not require sponsorship.",
            "We don't provide visa support.",
            "PwC does not intend to hire experienced or entry level job seekers who will need, now or in the future, PwC sponsorship through the H-1B lottery.",
        ]:
            self.assertIs(sponsorship(text), False, text)

    def test_true_phrasings(self):
        for text in [
            "We sponsor H-1B visas for qualified candidates.",
            "Visa sponsorship is available.",
            "- Visa sponsorships",
            "We are open to sponsoring work visas.",
            "H1B transfers are supported.",
            "Sponsorship available for the right candidate.",
            "We will sponsor your work authorization.",
        ]:
            self.assertIs(sponsorship(text), True, text)

    def test_undefined_when_not_mentioned(self):
        for text in ["", None, "We are hiring a Python developer. Remote in the US.",
                     "We sponsor tech meetups and conferences.",
                     "Our sponsorship of the marathon builds community.",
                     "Remote anywhere in the US", "As we hire globally we value diversity."]:
            self.assertIsNone(sponsorship(text), text)

    def test_negative_wins_over_positive(self):
        text = "We offer visa sponsorship for some roles. We do not sponsor visas for this role."
        self.assertIs(sponsorship(text), False)

    def test_evidence_is_the_deciding_sentence(self):
        out = jd_flags.detect("Intro text.\nVisa Sponsorship is not available for this position.\nMore text.")
        self.assertEqual(out["sponsorship_evidence"], "Visa Sponsorship is not available for this position.")
        self.assertEqual(jd_flags.detect("nothing here")["sponsorship_evidence"], "")

    def test_curly_apostrophe(self):
        self.assertIs(sponsorship("We don’t provide visa sponsorship."), False)

    def test_case_insensitive(self):
        self.assertIs(sponsorship("WE DO NOT SPONSOR H1B"), False)


class ClearanceTests(unittest.TestCase):
    def test_true_phrasings(self):
        for text in [
            "Active Secret clearance required.",
            "Must hold a US security clearance.",
            "Requires a Security Clearance.",
            "TS/SCI with polygraph preferred.",
            "Currently a US Citizen with capability to obtain and maintain a US Q clearance.",
            "Ability to pass a federal background investigation and eligibility requirements for access to classified information.",
            "Public Trust clearance level required.",
            "DoD clearance is a plus.",
        ]:
            self.assertTrue(clearance(text), text)

    def test_false_phrasings(self):
        for text in ["", None, "Clearance sale on all items.", "Inventory clearance experience.",
                     "Customs clearance of shipments.", "We run a standard background check.",
                     "Clear communication skills."]:
            self.assertFalse(clearance(text), text)

    def test_evidence(self):
        out = jd_flags.detect("Hello.\nActive Secret clearance required.")
        self.assertEqual(out["clearance_evidence"], "Active Secret clearance required.")


class CitizenshipTests(unittest.TestCase):
    def test_true_phrasings(self):
        for text in [
            "US citizenship is required.",
            "U.S. citizenship is required.",
            "Applicants must be a U.S. citizen or national, a lawful permanent resident, a refugee or asylee.",
            "This role involves access to export-controlled technical data.",
            "Eligibility (ITAR)",
            "Only US persons may apply.",
        ]:
            self.assertTrue(citizenship(text), text)

    def test_false_phrasings(self):
        for text in ["", None, "Export Control Regulations\n\nFor positions requiring access to controlled technology "
                     "subject to export control regulations, ServiceNow may be required to obtain export control approval.",
                     "We hire US and international candidates.", "Citizens of any country welcome."]:
            self.assertFalse(citizenship(text), text)

    def test_us_abbreviation_does_not_split_sentences(self):
        out = jd_flags.detect("Intro.\n- U.S. citizenship is required.")
        self.assertIn("citizenship is required", out["us_citizenship_evidence"])


class ShapeTests(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(sorted(jd_flags.detect("x")), sorted([
            "sponsorship", "sponsorship_evidence", "clearance", "clearance_evidence",
            "us_citizenship_required", "us_citizenship_evidence"]))


if __name__ == "__main__":
    unittest.main()
