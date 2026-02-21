import getpass
import requests

username = input("Plex username: ")
password = getpass.getpass("Plex password: ")

auth = {
    'user[login]': username,
    'user[password]': password
}

response = requests.post('https://plex.tv/users/sign_in.json', data=auth, headers={
    'X-Plex-Client-Identifier': 'Episeerr-TokenFetcher',
    'X-Plex-Product': 'Episeerr',
    'X-Plex-Version': '1.0'
})

if response.ok:
    token = response.json()['user']['authToken']
    print(f"\nYour Plex.tv auth token: {token}")
    print("\nThis token works for both:")
    print("  - Local Plex server access")
    print("  - Plex.tv watchlist API")
else:
    print(f"Error: {response.status_code} - {response.text}")
