# fpl_api.py
import requests
from utils import fuzzy_match

HEADERS = {
    "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 5.1; PRO 5 Build/LMY47D)",
    "accept-language": "en"
}

def get_current_gameweek():
    """
    Get the current gameweek from the FPL API.
    """
    url = 'https://fantasy.premierleague.com/api/bootstrap-static/'
    response = requests.get(url)
    if response.status_code == 200:
        data = response.json()
        current_gameweek = next(event['id'] for event in data['events'] if event['is_current'])
        return current_gameweek + 1
    else:
        raise Exception("Failed to fetch current gameweek from FPL API")

def get_player_info(player_name, expected_position=None):
    """
    Fetch player information (name, position, cost) from the FPL API.
    Supports exact match or fuzzy matching by name, with tie-breaking by 'selected_by_percent'.
    """
    url = 'https://fantasy.premierleague.com/api/bootstrap-static/'
    response = requests.get(url)
    if response.status_code != 200:
        raise Exception(f"Failed to fetch player info: {response.status_code}")
    
    data = response.json()
    players = data['elements']
    positions = data['element_types']

    # Map position IDs to names
    position_dict = {position['id']: position['singular_name'].lower() for position in positions}

    # Search for player by name and position if expected_position is provided
    player_matches = [
        {
            'name': f"{player['first_name']} {player['second_name']}" if player.get('first_name') and player.get('second_name') else None,
            'id': player['id'],
            'position': position_dict[player['element_type']],
            'now_cost': player['now_cost'],
            'selected_by_percent': float(player['selected_by_percent'])
        }
        for player in players
        if f"{player['first_name']} {player['second_name']}".lower() == player_name.lower()
        and (not expected_position or position_dict[player['element_type']] == expected_position.lower())
    ]

    # If an exact match is found, return player info
    if player_matches and player_matches[0]['name']:
        return player_matches[0]

    # Fuzzy matching fallback if no exact match found
    all_players = {
        f"{player['first_name']} {player['second_name']}": {
            'id': player['id'],
            'position': position_dict[player['element_type']],
            'now_cost': player['now_cost'],
            'selected_by_percent': float(player['selected_by_percent'])
        }
        for player in players if player.get('first_name') and player.get('second_name')  # Ensure names are available
    }

    # Filter players by expected position if provided
    if expected_position:
        all_players = {name: info for name, info in all_players.items() if info['position'].lower() == expected_position.lower()}

    if not all_players:
        return None

    # Perform fuzzy matching and sort by score and selected_by_percent
    matches = fuzzy_match(player_name, all_players)
    if matches:
        sorted_matches = sorted(matches, key=lambda match: (match[1], all_players[match[0]]['selected_by_percent']), reverse=True)
        closest_match = sorted_matches[0][0]
        player_info = all_players[closest_match]
        return {
            'name': closest_match,
            **player_info  # Add the name back into the dictionary
        }
    
    return None