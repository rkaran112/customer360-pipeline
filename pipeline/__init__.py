"""
Customer 360 pipeline package.

Each file is ONE stage of the pipeline:

    extract.py      STEP 1  read JSON / CSV / XML files
    standardize.py  STEP 2  same column names, clean values, correct types
    quality.py      STEP 3  data-quality rules -> clean rows + quarantine
    merge.py        STEP 4  join the 3 sources into one row per customer
    insights.py     STEP 5  segments, top customers, city and support analytics
    checks.py       STEP 6  prove nothing was lost before publishing

Helpers: config.py (settings), logger.py (logging), outputs.py (writing files),
diagram.py (pipeline picture for the dashboard), privacy.py (email masking).
"""
