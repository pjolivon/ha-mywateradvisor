"""Constants for the MyWaterAdvisor integration."""

DOMAIN = "mywateradvisor"

CONF_EMAIL = "email"
CONF_PASSWORD = "password"

# Portal is powered by the "Harmony Encore" customer portal platform.
BASE_URL = "https://customerportal-api.harmonyencoremdm.com"
PORTAL_ROOT_URL = "https://mywateradvisor2.com"

# Publicly-known fallback app id (reverse engineered from the portal's JS
# bundle by a third party). Used only if we can't extract it live.
KNOWN_APP_ID_FALLBACK = "3a869241-d476-40f6-a923-d789d63db11d"

# Hourly readings above this are treated as bad data (meter glitch / portal
# correction spike) and excluded from the running total.
ANOMALY_GALLONS_PER_HOUR = 2000

# Default length of a vacation scheduled via the Vacation Mode switch (as
# opposed to the schedule_vacation service, which takes an explicit range).
VACATION_DEFAULT_DAYS = 30

# The portal requires ConsumptionDailyLimit on every vacation — it 400s
# without one. Used when the switch (which has no way to prompt for a
# value) or the schedule_vacation service (daily_limit is optional there)
# doesn't supply one.
VACATION_DEFAULT_DAILY_LIMIT_GALLONS = 500

# The portal reports consumption only, never a dollar cost, so the cost
# backfill statistic (mywateradvisor:<account>_water_cost) computes its own
# USD sum from a per-gallon rate. This is only the initial value, seeding the
# "Water Price Per Gallon" number entity the first time the integration runs
# — after that, the entity's (persisted) value is what's actually used, and
# editing it in the UI is how you keep the rate in sync with your utility.
DEFAULT_WATER_PRICE_PER_GALLON = 0.00439167

