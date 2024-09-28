# utils.py
import os
import msal
from fuzzywuzzy import process

CACHE_FILE = 'token_cache.json'

def load_cache():
    """
    Load MSAL token cache from a file if it exists.
    """
    cache = msal.SerializableTokenCache()
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'r') as file:
            cache.deserialize(file.read())
    return cache

def save_cache(cache):
    """
    Save MSAL token cache to a file if its state has changed.
    """
    if cache.has_state_changed:
        with open(CACHE_FILE, 'w') as file:
            file.write(cache.serialize())

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
