from __future__ import annotations

import logging
import os
import uuid
import asyncio
import time
from aiohttp import ClientSession
from shutil import copyfileobj, shutil.Error
from typing import Iterator, Optional
from flask import abort, request, send_from_directory
from werkzeug.utils import secure_filename

from g4f import version, models
from g4f import get_last_provider, ChatCompletion
from g4f.cookies import get_cookies_dir
from g4f.errors import VersionNotFoundError
from g4f.typing import Cookies
from g4f.image import ImagePreview, ImageResponse, is_accepted_format, extract_data_uri
from g4f.requests.aiohttp import get_connector
from g4f.Provider import ProviderType, __providers__, __map__
from g4f.providers.base_provider import ProviderModelMixin, FinishReason
from g4f.providers.conversation import BaseConversation
from g4f import debug

logger = logging.getLogger(__name__)

# Define the directory for generated images
images_dir = "./generated_images"

# Function to ensure the images directory exists
def ensure_images_dir():
    if not os.path.exists(images_dir):
        os.makedirs(images_dir)

def ensure_har_cookies_dir():
    if not os.path.exists(get_cookies_dir()):
        os.makedirs(get_cookies_dir())

conversations: dict[dict[str, BaseConversation]] = {}

class Api:
    @staticmethod
    def get_models() -> list[str]:
        return models._all_models

    @staticmethod
    def get_provider_models(provider: str, api_key: str = None) -> list[dict]:
        if provider in __map__:
            provider: ProviderType = __map__[provider]
            if issubclass(provider, ProviderModelMixin):
                models = provider.get_models() if api_key is None else provider.get_models(api_key=api_key)
                return [
                    {
                        "model": model,
                        "default": model == provider.default_model,
                        "vision": getattr(provider, "default_vision_model", None) == model or model in getattr(provider, "vision_models", []),
                        "image": False if provider.image_models is None else model in provider.image_models,
                    }
                    for model in models
                ]
        return []

    @staticmethod
    def get_image_models() -> list[dict]:
        image_models = []
        index = []
        for provider in __providers__:
            if hasattr(provider, "image_models"):
                if hasattr(provider, "get_models"):
                    provider.get_models()
                parent = provider
                if hasattr(provider, "parent"):
                    parent = __map__[provider.parent]
                if parent.__name__ not in index:
                    for model in provider.image_models:
                        image_models.append({
                            "provider": parent.__name__,
                            "url": parent.url,
                            "label": parent.label if hasattr(parent, "label") else None,
                            "image_model": model,
                            "vision_model": getattr(parent, "default_vision_model", None)
                        })
                    index.append(parent.__name__)
            elif hasattr(provider, "default_vision_model") and provider.__name__ not in index:
                image_models.append({
                    "provider": provider.__name__,
                    "url": provider.url,
                    "label": provider.label if hasattr(provider, "label") else None,
                    "image_model": None,
                    "vision_model": provider.default_vision_model
                })
                index.append(provider.__name__)
        return image_models

    @staticmethod
    def get_providers() -> list[str]:
        return {
            provider.__name__: (provider.label if hasattr(provider, "label") else provider.__name__)
            + (" (Image Generation)" if hasattr(provider, "image_models") else "")
            + (" (Image Upload)" if getattr(provider, "default_vision_model", None) else "")
            + (" (WebDriver)" if "webdriver" in provider.get_parameters() else "")
            + (" (Auth)" if provider.needs_auth else "")
            for provider in __providers__
            if provider.working
        }

    @staticmethod
    def get_version():
        try:
            current_version = version.utils.current_version
        except VersionNotFoundError:
            current_version = None
        return {
            "version": current_version,
            "latest_version": version.utils.latest_version,
        }

    def serve_images(self, name):
        ensure_images_dir()
        return send_from_directory(os.path.abspath(images_dir), name)
    
    def load_har(self):
        length = request.content_length
        if length is not None and length > 50 * 1024 * 1024: #50MB max
            abort(413)

        if 'file' not in request.files:
            return 'No file uploaded', 500

        har_file = request.files['file']
        if har_file.filename == '':
            return 'No file name', 500

        ensure_har_cookies_dir()

        file_ext = os.path.splitext(har_file.filename)[1]
        if file_ext not in ['.txt', '.har']:
            return 'File type not supported', 500

        filename = secure_filename(har_file.filename)
        try:
            dst = open(os.path.join(get_cookies_dir(), filename), 'wxb')
            copyfileobj(har_file, dst):
            return 'Upload successful', 200
        except FileExistsError:
            return 'File already exists', 500
        except shutil.Error as e:
            return str(e), 500
        finally:
            dst.close()

        return 'Internal Server Error', 500

    def _prepare_conversation_kwargs(self, json_data: dict, kwargs: dict):
        model = json_data.get('model') or models.default
        provider = json_data.get('provider')
        messages = json_data['messages']
        api_key = json_data.get("api_key")
        if api_key is not None:
            kwargs["api_key"] = api_key
        if json_data.get('web_search'):
            if provider:
                kwargs['web_search'] = True
            else:
                from .internet import get_search_message
                messages[-1]["content"] = get_search_message(messages[-1]["content"])

        conversation_id = json_data.get("conversation_id")
        if conversation_id and provider in conversations and conversation_id in conversations[provider]:
            kwargs["conversation"] = conversations[provider][conversation_id]

        return {
            "model": model,
            "provider": provider,
            "messages": messages,
            "stream": True,
            "ignore_stream": True,
            "return_conversation": True,
            **kwargs
        }

    def _create_response_stream(self, kwargs: dict, conversation_id: str, provider: str) -> Iterator:
        if debug.logging:
            debug.logs = []
            print_callback = debug.log_handler
            def log_handler(text: str):
                debug.logs.append(text)
                print_callback(text)
            debug.log_handler = log_handler
        try:
            result = ChatCompletion.create(**kwargs)
            first = True
            if isinstance(result, ImageResponse):
                if first:
                    first = False
                    yield self._format_json("provider", get_last_provider(True))
                yield self._format_json("content", str(result))
            else:
                for chunk in result:
                    if first:
                        first = False
                        yield self._format_json("provider", get_last_provider(True))
                    if isinstance(chunk, BaseConversation):
                        if provider not in conversations:
                            conversations[provider] = {}
                        conversations[provider][conversation_id] = chunk
                        yield self._format_json("conversation", conversation_id)
                    elif isinstance(chunk, Exception):
                        logger.exception(chunk)
                        yield self._format_json("message", get_error_message(chunk))
                    elif isinstance(chunk, ImagePreview):
                        yield self._format_json("preview", chunk.to_string())
                    elif isinstance(chunk, ImageResponse):
                        images = asyncio.run(self._copy_images(chunk.get_list(), chunk.options.get("cookies")))
                        yield self._format_json("content", str(ImageResponse(images, chunk.alt)))
                    elif not isinstance(chunk, FinishReason):
                        yield self._format_json("content", str(chunk))
                    if debug.logs:
                        for log in debug.logs:
                            yield self._format_json("log", str(log))
                        debug.logs = []
        except Exception as e:
            logger.exception(e)
            yield self._format_json('error', get_error_message(e))

    async def _copy_images(self, images: list[str], cookies: Optional[Cookies] = None):
        ensure_images_dir()
        async with ClientSession(
            connector=get_connector(None, os.environ.get("G4F_PROXY")),
            cookies=cookies
        ) as session:
            async def copy_image(image: str) -> str:
                target = os.path.join(images_dir, f"{int(time.time())}_{str(uuid.uuid4())}")
                if image.startswith("data:"):
                    with open(target, "wb") as f:
                        f.write(extract_data_uri(image))
                else:
                    async with session.get(image) as response:
                        with open(target, "wb") as f:
                            async for chunk in response.content.iter_any():
                                f.write(chunk)
                with open(target, "rb") as f:
                    extension = is_accepted_format(f.read(12)).split("/")[-1]
                    extension = "jpg" if extension == "jpeg" else extension
                new_target = f"{target}.{extension}"
                os.rename(target, new_target)
                return f"/images/{os.path.basename(new_target)}"

            return await asyncio.gather(*[copy_image(image) for image in images])

    def _format_json(self, response_type: str, content):
        return {
            'type': response_type,
            response_type: content
        }

def get_error_message(exception: Exception) -> str:
    message = f"{type(exception).__name__}: {exception}"
    provider = get_last_provider()
    if provider is None:
        return message
    return f"{provider.__name__}: {message}"
