"""hindi_input.py tests - offline phonetic rules only (no network).

Run:  python tests/test_hindi_input.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hindi_input as h  # noqa: E402

first = lambda w: h.phonetic(w)[0]  # noqa: E731

# exceptions (very common name tokens)
assert first("singh") == "सिंह" and first("kumar") == "कुमार" and first("devi") == "देवी"
assert first("sharma") == "शर्मा" and first("gupta") == "गुप्ता" and first("ashutosh") == "आशुतोष"
# rules
assert first("chandra") == "चंद्र"           # nasal + C+r cluster, inherent final a
assert first("satya") == "सत्य"              # C+y cluster
assert first("priya") == "प्रिया"            # long final a after a single consonant
assert first("kamla") == "कमला"              # C+l keeps its inherent vowel
assert first("kanhaiya") == "कन्हैया"        # n+h joins
assert first("deepak") == "दीपक" and first("sooraj") == "सूरज"
assert first("arjun") == "अर्जुन"            # r as first member -> reph
assert first("johri") == "जौहरी"             # exception; rules alone would give जोहरी
# alternates cover the usual ambiguities
assert "सीता" in h.phonetic("sita") and "आनंद" in h.phonetic("anand") and "सूर्य" in h.phonetic("surya")
# phrases and pass-through
assert h.phonetic("ramesh kumar")[0] == "रमेश कुमार"
assert first("ram123") == "रम123" and h.phonetic("") == []
# suggest() never raises (falls back to rules when offline) and caches
cands = h.suggest("ashutosh johri")
assert cands and cands == h.suggest("ashutosh johri") and h.last_source in ("google", "rules")

print("hindi_input: ALL TESTS PASSED")
