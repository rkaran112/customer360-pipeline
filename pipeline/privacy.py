"""
PRIVACY - hide personal data on screens (decision D-17).

The dashboard is seen by many people; an email address is personal data.
We show only enough to recognise it:  amit.sharma@gmail.com -> am***@gmail.com
The full email stays only in the customer_360 export, for systems that need it.
"""


def mask_email(email):
    """Keep the first 2 letters and the domain, hide the rest."""
    if not isinstance(email, str) or "@" not in email:
        return "***"
    name, domain = email.split("@", 1)
    return f"{name[:2]}***@{domain}"
