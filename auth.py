# auth.py
import msal
from utils import load_cache, save_cache
from config import CLIENT_ID, AUTHORITY, SCOPE

def get_access_token():
    cache = load_cache()
    app = msal.PublicClientApplication(CLIENT_ID, authority=AUTHORITY, token_cache=cache)

    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPE, account=accounts[0])
        if 'access_token' in result:
            save_cache(cache)
            return result['access_token']

    flow = app.initiate_device_flow(scopes=SCOPE)
    if 'user_code' not in flow:
        raise ValueError("Failed to create device flow. Error: %s" % flow)

    print(f"Please go to {flow['verification_uri']} and enter the code: {flow['user_code']}")
    
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" in result:
        save_cache(cache)
        return result["access_token"]
    return None