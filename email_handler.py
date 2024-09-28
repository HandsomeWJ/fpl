# email_handler.py
import requests
from bs4 import BeautifulSoup
from fpl_api import get_player_info
from datetime import datetime, timedelta

def fetch_and_filter_emails(access_token):
    if not access_token:
        return []
    
    # print(f"Using access token: {access_token}")  # Logging access token for debugging
    headers = {'Authorization': f'Bearer {access_token}'}
    
    # Get current time and subtract 24 hours to get the cutoff time
    cutoff_time = (datetime.utcnow() - timedelta(days=1)).isoformat() + 'Z'  # 'Z' indicates UTC time

    # Use the Junk Mail folder for the query
    junk_folder_endpoint = (
        f"https://graph.microsoft.com/v1.0/me/mailFolders/junkemail/messages"
        f"?$filter=from/emailAddress/address eq 'admin@fantasyfootballfix.com' "
        f"and contains(subject, 'Elite XI Alert') "
        f"and receivedDateTime ge {cutoff_time}"
    )
    
    all_emails = []
    while junk_folder_endpoint:
        response = requests.get(junk_folder_endpoint, headers=headers)
        
        # Log response status and body
        # print(f"API Response Status: {response.status_code}")
        if response.status_code != 200:
            # print(f"API Error: {response.text}")
            return []  # Exit on error
        
        emails = response.json().get('value', [])
        all_emails.extend(emails)
        junk_folder_endpoint = response.json().get('@odata.nextLink')

    # Return all emails fetched
    return all_emails


def extract_players(html_content):
    """
    Parse the HTML content of the email to extract transferred-in and transferred-out players.
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    transferred_in, transferred_out = [], []
    current_section = None

    for element in soup.find_all(['p', 'table']):
        if element.name == 'p' and 'transferred in' in element.get_text().lower():
            current_section = 'in'
            continue
        elif element.name == 'p' and 'transferred out' in element.get_text().lower():
            current_section = 'out'
            continue
        elif element.name == 'p' and 'changed captain' in element.get_text().lower():
            current_section = None
            continue

        if element.name == 'table' and current_section:
            player_name_tag = element.find('h2')
            if player_name_tag:
                player_name = player_name_tag.get_text(strip=True)

                # Fetch player info (including position) from the API or team_players
                player_info = get_player_info(player_name)
                # print(f"Debug: player_info = {player_info}")  # Debugging line to check player_info structure

                if player_info:
                    if current_section == 'in':
                        # Check if the 'name' and 'position' keys exist before appending
                        if 'name' in player_info and 'position' in player_info:
                            transferred_in.append((player_info['name'], player_info['position']))
                        else:
                            print(f"Error: 'name' or 'position' key missing in player_info: {player_info}")
                    elif current_section == 'out':
                        if 'name' in player_info and 'position' in player_info:
                            transferred_out.append((player_info['name'], player_info['position']))
                        else:
                            print(f"Error: 'name' or 'position' key missing in player_info: {player_info}")

    return transferred_in, transferred_out


