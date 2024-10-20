#!/usr/bin/env python
import math
import os
import sys
from os import name
from pathlib import Path
from subprocess import Popen
from typing import Dict, NoReturn

from prawcore import ResponseException

from elevenLabsKeyRotator import APIKeyRotator
from reddit.subreddit import get_subreddit_threads
from utils import settings
from utils.cleanup import cleanup
from utils.console import print_markdown, print_step, print_substep
from utils.ffmpeg_install import ffmpeg_install
from utils.id import extract_id
from utils.version import checkversion
from video_creation.background import (
    chop_background,
    download_background_audio,
    download_background_video,
    get_background_config,
)
from video_creation.final_video import make_final_video
from video_creation.screenshot_downloader import get_screenshots_of_reddit_posts
from video_creation.voices import save_text_to_mp3
from video_creation.captionGen import main as caption_main

__VERSION__ = "3.3.0"

print(
    """
██████╗ ███████╗██████╗ ██████╗ ██╗████████╗    ██╗   ██╗██╗██████╗ ███████╗ ██████╗     ███╗   ███╗ █████╗ ██╗  ██╗███████╗██████╗
██╔══██╗██╔════╝██╔══██╗██╔══██╗██║╚══██╔══╝    ██║   ██║██║██╔══██╗██╔════╝██╔═══██╗    ████╗ ████║██╔══██╗██║ ██╔╝██╔════╝██╔══██╗
██████╔╝█████╗  ██║  ██║██║  ██║██║   ██║       ██║   ██║██║██║  ██║█████╗  ██║   ██║    ██╔████╔██║███████║█████╔╝ █████╗  ██████╔╝
██╔══██╗██╔══╝  ██║  ██║██║  ██║██║   ██║       ╚██╗ ██╔╝██║██║  ██║██╔══╝  ██║   ██║    ██║╚██╔╝██║██╔══██║██╔═██╗ ██╔══╝  ██╔══██╗
██║  ██║███████╗██████╔╝██████╔╝██║   ██║        ╚████╔╝ ██║██████╔╝███████╗╚██████╔╝    ██║ ╚═╝ ██║██║  ██║██║  ██╗███████╗██║  ██║
╚═╝  ╚═╝╚══════╝╚═════╝ ╚═════╝ ╚═╝   ╚═╝         ╚═══╝  ╚═╝╚═════╝ ╚══════╝ ╚═════╝     ╚═╝     ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝
"""
)
print_markdown(
    "### Thanks for using this tool! Feel free to contribute to this project on GitHub! If you have any questions, feel free to join my Discord server or submit a GitHub issue. You can find solutions to many common problems in the documentation: https://reddit-video-maker-bot.netlify.app/"
)
checkversion(__VERSION__)

reddit_id: str
reddit_object: Dict[str, str | list]


def main(POST_ID=None) -> None:
    global reddit_id, reddit_object
    reddit_object = get_subreddit_threads(POST_ID)
    reddit_id = extract_id(reddit_object)
    print_substep(f"Thread ID is {reddit_id}", style="bold blue")
    # rotate api key
    if settings.config["settings"]["tts"]["voice_choice"] == "elevenlabs":
        rotator = APIKeyRotator()
        rotator.run()
    length, number_of_comments = save_text_to_mp3(reddit_object)
    length = math.ceil(length)
    get_screenshots_of_reddit_posts(reddit_object, number_of_comments)
    bg_config = {
        "video": get_background_config("video"),
        "audio": get_background_config("audio"),
    }
    download_background_video(bg_config["video"])
    download_background_audio(bg_config["audio"])
    chop_background(bg_config, length, reddit_object)
    final_video_path = make_final_video(number_of_comments, length, reddit_object, bg_config)
    if (settings.config["settings"]["storymodemethod_cap_cut"] == True) and (settings.config["settings"]["storymode"] == True) and (settings.config["settings"]["storymodemethod"] == 1):
        print_step("Running Cap Cut text generation..")
        script_dir = os.path.dirname(os.path.abspath(__file__))
        font_path = os.path.join(script_dir, "fonts", "Rubik-Black.ttf")
        tmp_dir = os.path.join(script_dir, "assets", "temp")
        # Define the output path
        output_video_path = final_video_path
        
        # Rename the original file
        original_video_path = os.path.join(os.path.dirname(final_video_path), "original_" + os.path.basename(final_video_path))
        os.rename(final_video_path, original_video_path)
        
        # Call caption_main with the new paths
        caption_main(original_video_path, output_video_path, font_path, tmp_dir)

        # Delete the original video file after processing
        os.remove(original_video_path)
    print_step("Done! 🎉 The video is in the results folder 📁")

def run_many(times) -> None:
    for x in range(1, times + 1):
        print_step(
            f'on the {x}{("th", "st", "nd", "rd", "th", "th", "th", "th", "th", "th")[x % 10]} iteration of {times}'
        )
        main()
        Popen("cls" if name == "nt" else "clear", shell=True).wait()


def shutdown() -> NoReturn:
    if "reddit_id" in globals():
        print_markdown("## Clearing temp files")
        cleanup(reddit_id)

    print("Exiting...")
    sys.exit()


if __name__ == "__main__":
    if sys.version_info.major != 3 or sys.version_info.minor not in [10, 11]:
        print(
            "Hey! Congratulations, you've made it so far (which is pretty rare with no Python 3.10). Unfortunately, this program only works on Python 3.10. Please install Python 3.10 and try again."
        )
        sys.exit()
    ffmpeg_install()
    directory = Path().absolute()
    config = settings.check_toml(
        f"{directory}/utils/.config.template.toml", f"{directory}/config.toml"
    )
    config is False and sys.exit()

    if (
        not settings.config["settings"]["tts"]["tiktok_sessionid"]
        or settings.config["settings"]["tts"]["tiktok_sessionid"] == ""
    ) and config["settings"]["tts"]["voice_choice"] == "tiktok":
        print_substep(
            "TikTok voice requires a sessionid! Check our documentation on how to obtain one.",
            "bold red",
        )
        sys.exit()
    try:
        if config["reddit"]["thread"]["post_id"]:
            for index, post_id in enumerate(config["reddit"]["thread"]["post_id"].split("+")):
                index += 1
                print_step(
                    f'on the {index}{("st" if index % 10 == 1 else ("nd" if index % 10 == 2 else ("rd" if index % 10 == 3 else "th")))} post of {len(config["reddit"]["thread"]["post_id"].split("+"))}'
                )
                main(post_id)
                Popen("cls" if name == "nt" else "clear", shell=True).wait()
        elif config["settings"]["times_to_run"]:
            run_many(config["settings"]["times_to_run"])
        else:
            main()
    except KeyboardInterrupt:
        shutdown()
    except ResponseException:
        print_markdown("## Invalid credentials")
        print_markdown("Please check your credentials in the config.toml file")
        shutdown()
    except Exception as err:
        config["settings"]["tts"]["tiktok_sessionid"] = "REDACTED"
        config["settings"]["tts"]["elevenlabs_api_key"] = "REDACTED"
        print_step(
            f"Sorry, something went wrong with this version! Try again, and feel free to report this issue at GitHub or the Discord community.\n"
            f"Version: {__VERSION__} \n"
            f"Error: {err} \n"
            f'Config: {config["settings"]}'
        )
        raise err
