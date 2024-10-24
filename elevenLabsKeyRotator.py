import toml
from datetime import datetime, timedelta
import json
import os
from pathlib import Path

class APIKeyRotator:
    def __init__(self, config_path='config.toml', key_storage_path='api_keys.json'):
        self.config_path = config_path
        self.key_storage_path = key_storage_path
        self.api_keys = {
            'sk_yourElevenLabsKey': {'uses': 0, 'retired_date': None},
            'sk_otherElevenLabsKey': {'uses': 0, 'retired_date': None},
        }
        self.initialize_storage()

    def initialize_storage(self):
        # Initialize or load API keys storage
        if os.path.exists(self.key_storage_path):
            with open(self.key_storage_path, 'r') as f:
                self.api_keys = json.load(f)
        else:
            self.save_key_storage()

    def save_key_storage(self):
        with open(self.key_storage_path, 'w') as f:
            json.dump(self.api_keys, f, indent=4)

    def get_active_api_key(self):
        current_time = datetime.now()
        
        # Check for keys that can be reactivated
        for key, info in self.api_keys.items():
            if info['retired_date']:
                retired_date = datetime.fromisoformat(info['retired_date'])
                if current_time - retired_date >= timedelta(days=30):
                    info['retired_date'] = None
                    info['uses'] = 0

        # Find an available key
        for key, info in self.api_keys.items():
            if info['retired_date'] is None and info['uses'] < 8:
                return key

        raise Exception("No available API keys found!")

    def update_config(self, new_key):
        # Read the current config
        with open(self.config_path, 'r') as f:
            config = toml.load(f)

        # Update the API key
        config['settings']['tts']['elevenlabs_api_key'] = new_key

        # Write the updated config back
        with open(self.config_path, 'w') as f:
            toml.dump(config, f)

    def run(self):
        current_key = None

        # Load current config to get the current key
        with open(self.config_path, 'r') as f:
            config = toml.load(f)
            current_key = config['settings']['tts']['elevenlabs_api_key']

        # Update the use count for the current key
        if current_key in self.api_keys:
            self.api_keys[current_key]['uses'] += 1

            # Check if the current key needs to be rotated
            if self.api_keys[current_key]['uses'] >= 8:
                # Retire the current key
                self.api_keys[current_key]['retired_date'] = datetime.now().isoformat()

                # Get a new key and update the config
                new_key = self.get_active_api_key()
                self.update_config(new_key)

        # Save the updated state
        self.save_key_storage()

if __name__ == "__main__":
    rotator = APIKeyRotator()
    rotator.run()
