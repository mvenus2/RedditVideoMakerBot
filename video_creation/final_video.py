import multiprocessing
import os
import re
import tempfile
import textwrap
import threading
import time
from os.path import exists
from pathlib import Path
from typing import Dict, Final, Tuple

import ffmpeg
import translators
from PIL import Image, ImageDraw, ImageFont
from rich.console import Console
from rich.progress import track

from utils import settings
from utils.cleanup import cleanup
from utils.console import print_step, print_substep
from utils.fonts import getheight
from utils.id import extract_id
from utils.thumbnail import create_thumbnail
from utils.videos import save_data

console = Console()


class ProgressFfmpeg(threading.Thread):
    def __init__(self, vid_duration_seconds, progress_update_callback):
        threading.Thread.__init__(self, name="ProgressFfmpeg")
        self.stop_event = threading.Event()
        self.output_file = tempfile.NamedTemporaryFile(mode="w+", delete=False)
        self.vid_duration_seconds = vid_duration_seconds
        self.progress_update_callback = progress_update_callback

    def run(self):
        while not self.stop_event.is_set():
            latest_progress = self.get_latest_ms_progress()
            if latest_progress is not None:
                completed_percent = latest_progress / self.vid_duration_seconds
                self.progress_update_callback(completed_percent)
            time.sleep(1)

    def get_latest_ms_progress(self):
        lines = self.output_file.readlines()

        for line in lines:
            if "out_time_ms" in line:
                out_time_ms_str = line.split("=")[1].strip()
                if out_time_ms_str.isnumeric():
                    return float(out_time_ms_str) / 1000000.0
                else:
                    return None
        return None

    def stop(self):
        self.stop_event.set()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args, **kwargs):
        self.stop()


def name_normalize(name: str) -> str:
    name = re.sub(r'[?\\"%*:|<>]', "", name)
    name = re.sub(r"( [w,W]\s?\/\s?[o,O,0])", r" without", name)
    name = re.sub(r"( [w,W]\s?\/)", r" with", name)
    name = re.sub(r"(\d+)\s?\/\s?(\d+)", r"\1 of \2", name)
    name = re.sub(r"(\w+)\s?\/\s?(\w+)", r"\1 or \2", name)
    name = re.sub(r"\/", r"", name)

    lang = settings.config["reddit"]["thread"]["post_lang"]
    if lang:
        print_substep("Translating filename...")
        return translators.translate_text(name, translator="google", to_language=lang)
    return name


def prepare_background(reddit_id: str, W: int, H: int) -> str:
    output_path = f"assets/temp/{reddit_id}/background_noaudio.mp4"
    output = (
        ffmpeg.input(f"assets/temp/{reddit_id}/background.mp4")
        .filter("crop", f"ih*({W}/{H})", "ih")
        .output(
            output_path,
            an=None,
            **{
                "c:v": "h264",
                "b:v": "20M",
                "b:a": "192k",
                "threads": multiprocessing.cpu_count(),
            },
        )
        .overwrite_output()
    )
    try:
        output.run(quiet=True)
    except ffmpeg.Error as e:
        print(e.stderr.decode("utf8"))
        exit(1)
    return output_path


def get_text_height(draw, text, font, max_width):
    lines = textwrap.wrap(text, width=max_width)
    return sum(draw.textbbox((0, 0), line, font=font)[3] for line in lines)


def create_fancy_thumbnail(image, text, text_color, padding, wrap=35):
    print_step(f"Creating fancy thumbnail for: {text}")
    font_title_size = 47
    font = ImageFont.truetype(os.path.join("fonts", "Roboto-Bold.ttf"), font_title_size)
    image_width, image_height = image.size

    draw = ImageDraw.Draw(image)
    text_height = get_text_height(draw, text, font, wrap)
    lines = textwrap.wrap(text, width=wrap)
    new_image_height = image_height + text_height + padding * (len(lines) - 1) - 50

    top_part_height = image_height // 2
    bottom_part_height = image_height - top_part_height - 1
    new_middle_height = max(1, new_image_height - top_part_height - bottom_part_height)

    top_part = image.crop((0, 0, image_width, top_part_height))
    middle_part = image.crop((0, top_part_height, image_width, top_part_height + 1))
    bottom_part = image.crop((0, top_part_height + 1, image_width, image_height))

    middle_part = middle_part.resize((image_width, new_middle_height))

    new_image = Image.new("RGBA", (image_width, new_image_height))
    new_image.paste(top_part, (0, 0))
    new_image.paste(middle_part, (0, top_part_height))
    new_image.paste(bottom_part, (0, top_part_height + new_middle_height))

    draw = ImageDraw.Draw(new_image)
    y = top_part_height + padding
    for line in lines:
        draw.text((120, y), line, font=font, fill=text_color, align="left")
        y += get_text_height(draw, line, font, wrap) + padding

    username_font = ImageFont.truetype(os.path.join("fonts", "Roboto-Bold.ttf"), 30)
    draw.text(
        (205, 825),
        settings.config["settings"]["channel_name"],
        font=username_font,
        fill=text_color,
        align="left",
    )

    return new_image


def merge_background_audio(audio: ffmpeg, reddit_id: str):
    background_audio_volume = settings.config["settings"]["background"]["background_audio_volume"]
    if background_audio_volume == 0:
        return audio

    bg_audio = ffmpeg.input(f"assets/temp/{reddit_id}/background.mp3").filter(
        "volume",
        background_audio_volume,
    )
    merged_audio = ffmpeg.filter([audio, bg_audio], "amix", duration="longest")
    return merged_audio


def make_final_video(
    number_of_clips: int,
    length: int,
    reddit_obj: dict,
    background_config: Dict[str, Tuple],
):
    """Gathers audio clips, gathers all screenshots, stitches them together and saves the final video to assets/temp
    Args:
        number_of_clips (int): Index to end at when going through the screenshots'
        length (int): Length of the video
        reddit_obj (dict): The reddit object that contains the posts to read.
        background_config (Tuple[str, str, str, Any]): The background config to use.
    """
    # Initial setup
    W, H, opacity, reddit_id, allow_only_tts_folder = initial_setup(reddit_obj)
    print_step("Creating the final video 🎥")

    # Prepare background clip
    background_clip = prepare_background_clip(reddit_id, W, H)

    # Gather audio clips
    audio_clips = gather_audio_clips(number_of_clips, reddit_id, reddit_obj)

    # Concatenate audio clips
    final_audio_path = f"assets/temp/{reddit_id}/audio.mp3"
    concatenate_audio_clips(audio_clips, final_audio_path)

    # Log video length
    console.log(f"[bold green] Video Will Be: {length} Seconds Long")

    # Prepare final audio
    audio = ffmpeg.input(final_audio_path)
    final_audio = merge_background_audio(audio, reddit_id)

    # Create title image
    title_image_path = create_title_image(reddit_obj, reddit_id)

    # Prepare image clips
    screenshot_width = int((W * 45) // 100)
    image_clips = [ffmpeg.input(title_image_path)["v"].filter("scale", screenshot_width, -1)]

    # Gather audio clip durations
    audio_clips_durations = get_audio_clips_durations(number_of_clips, reddit_id)

    # Overlay images on background
    background_clip = overlay_images_on_background(
        background_clip,
        image_clips,
        audio_clips_durations,
        reddit_id,
        number_of_clips,
        opacity,
        screenshot_width,
    )

    # Prepare filename and paths
    title, idx, filename, subreddit = prepare_file_info(reddit_obj)

    # Create result folders if necessary
    create_result_folders(subreddit, allow_only_tts_folder)

    # Create thumbnail
    create_thumbnail_image(reddit_id, reddit_obj, subreddit)

    # Add background credit
    background_clip = add_background_credit(background_clip, background_config)

    # Scale background clip
    background_clip = background_clip.filter("scale", W, H)
    print_step("Rendering the video 🎥")

    # Render the main video
    default_path = f"results/{subreddit}"
    video_path = os.path.join(default_path, f"{filename}.mp4")
    render_video(background_clip, final_audio, video_path, length)

    # Render the Only TTS video if enabled
    if allow_only_tts_folder:
        only_tts_path = os.path.join(default_path, "OnlyTTS", f"{filename}.mp4")
        print_step("Rendering the Only TTS Video 🎥")
        render_video(background_clip, audio, only_tts_path, length)

    # Save data and cleanup
    save_data(subreddit, f"{filename}.mp4", title, idx, background_config["video"][2])
    print_step("Removing temporary files 🗑")
    cleanups = cleanup(reddit_id)
    print_substep(f"Removed {cleanups} temporary files 🗑")
    return video_path


def initial_setup(reddit_obj: dict):
    W = int(settings.config["settings"]["resolution_w"])
    H = int(settings.config["settings"]["resolution_h"])
    opacity = settings.config["settings"]["opacity"]
    reddit_id = extract_id(reddit_obj)
    allow_only_tts_folder = (
        settings.config["settings"]["background"]["enable_extra_audio"]
        and settings.config["settings"]["background"]["background_audio_volume"] != 0
    )
    return W, H, opacity, reddit_id, allow_only_tts_folder


def prepare_background_clip(reddit_id: str, W: int, H: int):
    background_path = prepare_background(reddit_id, W=W, H=H)
    return ffmpeg.input(background_path)


def gather_audio_clips(number_of_clips: int, reddit_id: str, reddit_obj: dict):
    audio_clips = []
    storymode = settings.config["settings"]["storymode"]
    if number_of_clips == 0 and storymode == "false":
        print("No audio clips to gather. Please use a different TTS or post.")
        exit()
    if storymode:
        if settings.config["settings"]["storymodemethod"] == 0:
            audio_clips = [ffmpeg.input(f"assets/temp/{reddit_id}/mp3/title.mp3")]
            audio_clips.insert(1, ffmpeg.input(f"assets/temp/{reddit_id}/mp3/postaudio.mp3"))
        elif settings.config["settings"]["storymodemethod"] == 1:
            audio_clips = [
                ffmpeg.input(f"assets/temp/{reddit_id}/mp3/postaudio-{i}.mp3")
                for i in track(range(number_of_clips + 1), "Collecting the audio files...")
            ]
            audio_clips.insert(0, ffmpeg.input(f"assets/temp/{reddit_id}/mp3/title.mp3"))
    else:
        audio_clips = [
            ffmpeg.input(f"assets/temp/{reddit_id}/mp3/{i}.mp3") for i in range(number_of_clips)
        ]
        audio_clips.insert(0, ffmpeg.input(f"assets/temp/{reddit_id}/mp3/title.mp3"))
    return audio_clips


def concatenate_audio_clips(audio_clips, output_path):
    audio_concat = ffmpeg.concat(*audio_clips, a=1, v=0)
    ffmpeg.output(
        audio_concat, output_path, **{"b:a": "192k"}
    ).overwrite_output().run(quiet=True)


def create_title_image(reddit_obj: dict, reddit_id: str):
    title_template = Image.open("assets/title_template.png")
    title = name_normalize(reddit_obj["thread_title"])
    font_color = "#000000"
    padding = 5
    title_img = create_fancy_thumbnail(title_template, title, font_color, padding)
    Path(f"assets/temp/{reddit_id}/png").mkdir(parents=True, exist_ok=True)
    title_img.save(f"assets/temp/{reddit_id}/png/title.png")
    return f"assets/temp/{reddit_id}/png/title.png"


def get_audio_clips_durations(number_of_clips: int, reddit_id: str):
    audio_clips_durations = []
    storymode = settings.config["settings"]["storymode"]
    if storymode:
        if settings.config["settings"]["storymodemethod"] == 0:
            audio_clips_durations = [
                float(ffmpeg.probe(f"assets/temp/{reddit_id}/mp3/title.mp3")["format"]["duration"]),
                float(ffmpeg.probe(f"assets/temp/{reddit_id}/mp3/postaudio.mp3")["format"]["duration"]),
            ]
        elif settings.config["settings"]["storymodemethod"] == 1:
            audio_clips_durations = [
                float(ffmpeg.probe(f"assets/temp/{reddit_id}/mp3/postaudio-{i}.mp3")["format"]["duration"])
                for i in range(number_of_clips + 1)
            ]
            audio_clips_durations.insert(
                0,
                float(ffmpeg.probe(f"assets/temp/{reddit_id}/mp3/title.mp3")["format"]["duration"]),
            )
    else:
        audio_clips_durations = [
            float(ffmpeg.probe(f"assets/temp/{reddit_id}/mp3/{i}.mp3")["format"]["duration"])
            for i in range(number_of_clips)
        ]
        audio_clips_durations.insert(
            0,
            float(ffmpeg.probe(f"assets/temp/{reddit_id}/mp3/title.mp3")["format"]["duration"]),
        )
    return audio_clips_durations


def overlay_images_on_background(
    background_clip,
    image_clips,
    audio_clips_durations,
    reddit_id,
    number_of_clips,
    opacity,
    screenshot_width,
):
    current_time = 0
    storymode = settings.config["settings"]["storymode"]
    if storymode:
        if settings.config["settings"]["storymodemethod"] == 0:
            image_clips.insert(
                1,
                ffmpeg.input(f"assets/temp/{reddit_id}/png/story_content.png").filter(
                    "scale", screenshot_width, -1
                ),
            )
            for i in range(2):
                background_clip = background_clip.overlay(
                    image_clips[i],
                    enable=f"between(t,{current_time},{current_time + audio_clips_durations[i]})",
                    x="(main_w-overlay_w)/2",
                    y="(main_h-overlay_h)/2",
                )
                current_time += audio_clips_durations[i]
        elif settings.config["settings"]["storymodemethod"] == 1:
            if settings.config["settings"]["storymodemethod_cap_cut"] == False:
                for i in track(range(0, number_of_clips + 1), "Collecting the image files..."):
                    image_clips.append(
                        ffmpeg.input(f"assets/temp/{reddit_id}/png/img{i}.png")["v"].filter(
                            "scale", screenshot_width, -1
                        )
                    )
                    background_clip = background_clip.overlay(
                        image_clips[i],
                        enable=f"between(t,{current_time},{current_time + audio_clips_durations[i]})",
                        x="(main_w-overlay_w)/2",
                        y="(main_h-overlay_h)/2",
                    )
                    current_time += audio_clips_durations[i]
            else:
                for i in track(range(0, number_of_clips + 1), "Collecting the image files..."):
                    # Create a transparent image for other clips
                    transparent_image = Image.new('RGBA', (screenshot_width, screenshot_width), (0, 0, 0, 0))
                    transparent_image.save(f"assets/temp/{reddit_id}/png/trs{i}.png")

                    image_clips.append(
                        ffmpeg.input(f"assets/temp/{reddit_id}/png/trs{i}.png")["v"].filter(
                            "scale", screenshot_width, -1
                        )
                    )
                    background_clip = background_clip.overlay(
                        image_clips[i],
                        enable=f"between(t,{current_time},{current_time + audio_clips_durations[i]})",
                        x="(main_w-overlay_w)/2",
                        y="(main_h-overlay_h)/2",
                    )
                    current_time += audio_clips_durations[i]
    else:
        for i in range(0, number_of_clips + 1):
            image_clips.append(
                ffmpeg.input(f"assets/temp/{reddit_id}/png/comment_{i}.png")["v"].filter(
                    "scale", screenshot_width, -1
                )
            )
            image_overlay = image_clips[i].filter("colorchannelmixer", aa=opacity)
            background_clip = background_clip.overlay(
                image_overlay,
                enable=f"between(t,{current_time},{current_time + audio_clips_durations[i]})",
                x="(main_w-overlay_w)/2",
                y="(main_h-overlay_h)/2",
            )
            current_time += audio_clips_durations[i]
    return background_clip


def prepare_file_info(reddit_obj: dict):
    title = extract_id(reddit_obj, "thread_title")
    idx = extract_id(reddit_obj)
    filename = f"{name_normalize(title)[:251]}"
    subreddit = settings.config["reddit"]["thread"]["subreddit"]
    return title, idx, filename, subreddit


def create_result_folders(subreddit: str, allow_only_tts_folder: bool):
    if not exists(f"./results/{subreddit}"):
        print_substep("The 'results' folder could not be found so it was automatically created.")
        os.makedirs(f"./results/{subreddit}")
    if allow_only_tts_folder and not exists(f"./results/{subreddit}/OnlyTTS"):
        print_substep("The 'OnlyTTS' folder could not be found so it was automatically created.")
        os.makedirs(f"./results/{subreddit}/OnlyTTS")


def create_thumbnail_image(reddit_id: str, reddit_obj: dict, subreddit: str):
    settingsbackground = settings.config["settings"]["background"]
    if settingsbackground["background_thumbnail"]:
        if not exists(f"./results/{subreddit}/thumbnails"):
            print_substep(
                "The 'results/thumbnails' folder could not be found so it was automatically created."
            )
            os.makedirs(f"./results/{subreddit}/thumbnails")
        first_image = next(
            (file for file in os.listdir("assets/backgrounds") if file.endswith(".png")),
            None,
        )
        if first_image is None:
            print_substep("No png files found in assets/backgrounds", "red")
        else:
            font_family = settingsbackground["background_thumbnail_font_family"]
            font_size = settingsbackground["background_thumbnail_font_size"]
            font_color = settingsbackground["background_thumbnail_font_color"]
            thumbnail = Image.open(f"assets/backgrounds/{first_image}")
            width, height = thumbnail.size
            title_thumb = reddit_obj["thread_title"]
            thumbnail_save = create_thumbnail(
                thumbnail,
                font_family,
                font_size,
                font_color,
                width,
                height,
                title_thumb,
            )
            thumbnail_save.save(f"./assets/temp/{reddit_id}/thumbnail.png")
            print_substep(
                f"Thumbnail - Building Thumbnail in assets/temp/{reddit_id}/thumbnail.png"
            )
    else:
        print_substep("Thumbnail creation is disabled in settings.")


def add_background_credit(background_clip, background_config):
    text = f"Background by {background_config['video'][2]}"
    return ffmpeg.drawtext(
        background_clip,
        text=text,
        x=f"(w-text_w)",
        y=f"(h-text_h)",
        fontsize=5,
        fontcolor="White",
        fontfile=os.path.join("fonts", "Roboto-Regular.ttf"),
    )


def render_video(background_clip, final_audio, path, length):
    from tqdm import tqdm

    pbar = tqdm(total=100, desc="Progress: ", bar_format="{l_bar}{bar}", unit=" %")

    def on_update_example(progress) -> None:
        status = round(progress * 100, 2)
        old_percentage = pbar.n
        pbar.update(status - old_percentage)

    with ProgressFfmpeg(length, on_update_example) as progress:
        try:
            ffmpeg.output(
                background_clip,
                final_audio,
                path,
                f="mp4",
                **{
                    "c:v": "h264",
                    "b:v": "20M",
                    "b:a": "192k",
                    "threads": multiprocessing.cpu_count(),
                },
            ).overwrite_output().global_args("-progress", progress.output_file.name).run(
                quiet=True,
                overwrite_output=True,
                capture_stdout=False,
                capture_stderr=False,
            )
        except ffmpeg.Error as e:
            print(e.stderr.decode("utf8"))
            exit(1)
    old_percentage = pbar.n
    pbar.update(100 - old_percentage)
    pbar.close()
