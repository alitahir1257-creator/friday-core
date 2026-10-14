"""Compatibility entry point for the FRIDAY Core desktop app."""

from friday.runtime_config import load_gemini_api_key_from_config
from friday.app import main


load_gemini_api_key_from_config()

if __name__ == "__main__":
    main()
