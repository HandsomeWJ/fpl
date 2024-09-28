# utils.py
import os
import msal
from fuzzywuzzy import process

CACHE_FILE = 'token_cache.json'

def load_cache():
    """
    Load MSAL token cache from GitHub Secret (TOKEN_CACHE_JSON) or a local file.
    """
    cache = msal.SerializableTokenCache()
    
    # First, try loading the token cache from an environment variable (GitHub Secret)
    token_cache_json = os.getenv('TOKEN_CACHE_JSON')
    if token_cache_json:
        print("Loading token cache from environment variable.")
        cache.deserialize(token_cache_json)
    # Fallback to loading from a file (useful for local testing)
    elif os.path.exists(CACHE_FILE):
        print("Loading token cache from local file.")
        with open(CACHE_FILE, 'r') as file:
            cache.deserialize(file.read())
    return cache

def save_cache(cache):
    """
    Save MSAL token cache to an environment variable or file if its state has changed.
    """
    if cache.has_state_changed:
        # Save the cache back into a file for local use
        with open(CACHE_FILE, 'w') as file:
            file.write(cache.serialize())
        
        # Optionally, you could save it back as an environment variable or GitHub Secret (not typically done automatically).
        # However, in GitHub Actions, this step is typically unnecessary unless you want to persist cache between workflows.
        print("Token cache state has changed and saved.")

def fuzzy_match(player_name, all_players):
    """
    Perform fuzzy matching on player names.
    
    Args:
        player_name (str): The name of the player to search for.
        all_players (dict): Dictionary of all players' full names and their info.
        
    Returns:
        list: List of tuples containing (matched player name, fuzzy score).
    """
    # Perform fuzzy matching
    matches = process.extract(player_name, all_players.keys(), limit=10)
    return matches
