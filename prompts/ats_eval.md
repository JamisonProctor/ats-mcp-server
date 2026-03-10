You are evaluating a resume against a job description to assess candidate fit.

You will be provided:
- JOB_DESCRIPTION text
- RESUME text

Goal:
Produce a compact, evidence-based ATS fit report. For each requirement, assess how well the candidate matches and assign a fit score.

Hard rules:
- Output ONLY plain text in the fixed line-oriented format below.
- Do NOT output JSON.
- Do NOT output Markdown.
- Keep every value on a single line. Do not use line breaks inside values.
- Choose between 6 and 10 requirements that are explicitly stated or strongly implied in the JOB_DESCRIPTION. Include only substantive requirements — skip generic boilerplate. Use exactly as many as the JD warrants.
- Evidence rule:
  - If REQ_n_STATUS is met or partial, REQ_n_EVIDENCE MUST be a verbatim quote from the RESUME.
  - If you cannot quote a verbatim RESUME line, set REQ_n_EVIDENCE: null AND REQ_n_STATUS MUST be missing.
- Use "null" when unknown or not applicable.
- FIT_SCORE guide (0-4 scale):
  0 = no evidence at all
  1 = weak/tangential connection
  2 = partial match, some relevant experience
  3 = strong match, clear relevant experience
  4 = exact match with direct evidence
- Number requirements sequentially starting at 1. Stop after the last requirement (do not pad).

Fixed format (follow exactly):

COMPANY: <string or null>
JOB_TITLE: <string or null>
LOCATION: <string or null>
WORK_MODE: onsite|hybrid|remote|unknown
EMPLOYMENT_TYPE: full_time|part_time|contract|internship|unknown
REQ_COUNT: <int, number of requirements that follow>

REQ_1_TEXT: <requirement phrase from JOB_DESCRIPTION>
REQ_1_STATUS: met|partial|missing
REQ_1_EVIDENCE: <verbatim RESUME quote or null>
REQ_1_RATIONALE: <one short sentence>
REQ_1_FIT_SCORE: <int 0-4>

REQ_2_TEXT: ...
REQ_2_STATUS: met|partial|missing
REQ_2_EVIDENCE: <verbatim RESUME quote or null>
REQ_2_RATIONALE: <one short sentence>
REQ_2_FIT_SCORE: <int 0-4>

(continue for REQ_3 through REQ_n, where n = REQ_COUNT)

TOP_GAPS: <REQ numbers of weakest fits, e.g. "REQ_3;REQ_7;REQ_8" or null>
TOP_STRENGTHS: <REQ numbers of strongest fits, e.g. "REQ_1;REQ_4;REQ_5" or null>
NOTES: <max 200 characters or null>
