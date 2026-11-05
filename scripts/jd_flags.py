"""Keyword based flags for a job description. No model involved.

detect(description) returns:
  sponsorship: True  = the JD says the employer sponsors work visas (H-1B etc)
               False = the JD says it does not sponsor / cannot consider people who need it
               None  = not mentioned (undefined)
  sponsorship_evidence: the sentence that decided it ("" when None)
  clearance: True if the JD mentions a security clearance, else False
  clearance_evidence: the first matching sentence ("" when False)
  us_citizenship_required: True if the JD requires US citizenship or US-person status (ITAR,
               export control), else False
  us_citizenship_evidence: the first matching sentence

If a JD has both a positive and a negative sponsorship sentence, the negative wins,
because that is the one that makes an application pointless.
"""
import re

SPONSOR = r"sponsor(?:s|ed|ing|ship|ships)?"
IMMIGRATION = (r"(?:visas?|h-?1-?b|h-?1b|work (?:authori[sz]ation|permit)|immigration|green card|"
               r"employment[- ]based|work visas?)")
NEGATION = (r"(?:no|not|never|without|unable|cannot|can't|can not|won't|will not|do not|does not|don't|"
            r"doesn't|isn't|aren't|not able|not currently|not in a position|ineligible)")

# negation near a sponsor word ("we do not sponsor", "no visa sponsorship", "unable to consider
# candidates who require sponsorship", "without the need for sponsorship")
NEGATIVE_SPONSORSHIP = [
    re.compile(r"\b" + NEGATION + r"\b[^.;]{0,140}\b" + SPONSOR + r"\b"),
    re.compile(r"\b" + SPONSOR + r"\b[^.;]{0,40}\b(?:is |are |will be )?(?:not|unavailable|n't)\b"),
    re.compile(r"\b" + NEGATION + r"\b[^.;]{0,60}\b(?:support|provide|offer|assist)\b[^.;]{0,30}\b" + IMMIGRATION),
]
POSITIVE_SPONSORSHIP = [
    re.compile(r"\b" + SPONSOR + r"\b[^.;]{0,80}\b" + IMMIGRATION + r"\b"),
    re.compile(r"\b" + IMMIGRATION + r"\b[^.;]{0,80}\b" + SPONSOR + r"\b"),
    re.compile(r"\bsponsorship (?:is |may be |will be |can be )?(?:available|offered|provided)\b"),
    re.compile(r"\bh-?1-?b\b[^.;]{0,30}\b(?:transfer|transfers|support|supported|available)\b"),
]

CLEARANCE = [
    re.compile(r"\bsecurity clearances?\b"),
    re.compile(r"\b(?:u\.?s\.?|united states|government|federal|dod|active|current|interim|valid|"
               r"secret|top secret|ts|public trust)\s+(?:security\s+)?clearances?\b"),
    re.compile(r"\bclearances? (?:level|required|eligibility|process|investigation)\b"),
    re.compile(r"\bts\s*/\s*sci\b|\btop secret\b|\bpublic trust\b|\bpolygraph\b"),
    re.compile(r"\b(?:obtain|hold|maintain|possess|have|eligible for)\b[^.;]{0,30}\bclearance\b"),
    # clearance requirements that never use the word
    re.compile(r"\baccess to classified (?:information|material|systems|data)\b|\bclassified (?:work|programs?)\b"),
    re.compile(r"\b(?:federal|government|security) background (?:investigation|check)\b"),
]
CITIZENSHIP = [
    re.compile(r"\b(?:u\.?s\.?|united states) citizen(?:ship)?\b[^.;]{0,50}\b(?:required|only|needed|necessary)\b"),
    re.compile(r"\b(?:must|need to|required to|have to|currently) (?:be )?an? (?:u\.?s\.?|united states) (?:citizen|national)\b"),
    re.compile(r"\bitar\b|\bexport[- ]controlled\b|\bu\.?s\.? persons?\b"),
]
# "clearance" alone is too common (inventory clearance, customs clearance), so every pattern
# above needs a security or government word next to it.


def _normalize(text):
    text = (text or "").replace("\u2019", "'").replace("\u2018", "'").replace("\u2011", "-").replace("\u2013", "-")
    # "U.S." must not look like the end of a sentence
    return re.sub(r"\b([Uu])\.([Ss])\.", r"\1\2", text)


def _sentences(text):
    parts = re.split(r"(?<=[.!?])\s+|\n+", _normalize(text))
    return [" ".join(p.split()) for p in parts if p.strip()]


def _first_match(sentences, patterns):
    """First sentence matching the highest priority pattern (patterns are listed in priority order)."""
    for pattern in patterns:
        for sentence in sentences:
            if pattern.search(sentence.lower()):
                return sentence
    return ""


def detect(description):
    sentences = _sentences(description)
    negative = _first_match(sentences, NEGATIVE_SPONSORSHIP)
    positive = "" if negative else _first_match(sentences, POSITIVE_SPONSORSHIP)
    clearance = _first_match(sentences, CLEARANCE)
    citizenship = _first_match(sentences, CITIZENSHIP)
    return {
        "sponsorship": False if negative else (True if positive else None),
        "sponsorship_evidence": negative or positive,
        "clearance": bool(clearance),
        "clearance_evidence": clearance,
        "us_citizenship_required": bool(citizenship),
        "us_citizenship_evidence": citizenship,
    }
