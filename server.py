import os
import audioop
import base64
from flask import Flask
from flask_sock import Sock
from twilio.twiml.voice_response import VoiceResponse, Connect
from openai import AsyncOpenAI
from dotenv import load_dotenv
import vosk
import json
import requests
import asyncio
import websockets
import shutil
import subprocess


client = AsyncOpenAI()

def is_installed(lib_name):
    return shutil.which(lib_name) is not None

load_dotenv()

model = vosk.Model('model')

app = Flask(__name__)
sock = Sock(app)

CL = '\x1b[0K'
BS = '\x08'

client = AsyncOpenAI()

@sock.route('/echo')
def echo(ws):
    print("Receiving socket events")
    rec = vosk.KaldiRecognizer(model, 16000)
    while True:
        message = ws.receive()
        packet = json.loads(message)
        if packet['event'] == 'start':
            print('Streaming is starting')
        elif packet['event'] == 'stop':
            print('\nStreaming has stopped')
        elif packet['event'] == 'media':
            streamSid = packet['streamSid']
            audio = base64.b64decode(packet['media']['payload'])
            audio = audioop.ulaw2lin(audio, 2)
            audio = audioop.ratecv(audio, 2, 1, 8000, 16000, None)[0]
            if rec.AcceptWaveform(audio):
                r = json.loads(rec.Result())
                user_query = r['text'];
                if user_query != "":
                  print(user_query)
                  asyncio.run(chat_completion(user_query, ws, streamSid))
            else:
                r = json.loads(rec.PartialResult())
                print(r['partial'] + BS * len(r['partial']), end='')

@app.route("/test_call", methods = ['POST'])
def generate_test_call():
    response = VoiceResponse()
    response.pause(length=3)
    response.say('What kinds of dogs are ideal for competitive agility?')
    response.pause(length=30)
    return str(response)

@app.route("/start")
def generate_twiml():
    response = VoiceResponse()
    connect = Connect()
    connect.stream(
        name="Audio Stream",
        url=f'wss://{os.environ.get("NGROK_URL")}/echo'
    )
    response.append(connect)
    return str(response)

async def chat_completion(prompt, socket, streamSid):
  print("Getting completion: " + prompt)
  response_stream = await client.chat.completions.create(
      model="gpt-4",
      messages=[
          {"role": "system", "content": "You are a helpful assistant chatting with a user on the phone. Keep your responses cheerful and brief."},
          {"role": "user", "content": prompt}
      ],
      stream=True
  )

  async def text_iterator():
    async for part in response_stream:
      delta = part.choices[0].delta
      if delta:
        yield delta.content
      else:
          break
        
  await text_to_speech_input_streaming(os.environ.get('XI_VOICE_ID'), text_iterator(), socket, streamSid)
  
async def text_to_speech_input_streaming(voice_id, text_iterator, socket, streamSid):
    """Send text to ElevenLabs API and stream the returned audio."""
    uri = f"wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input?model_id=eleven_monolingual_v1"

    async with websockets.connect(uri) as websocket:
        await websocket.send(json.dumps({
            "text": " ",
            "voice_settings": {"stability": 0.5, "similarity_boost": True},
            "xi_api_key": os.environ.get("XI_API_KEY"),
        }))

        async def listen():
            """Listen to the websocket for audio data and stream it."""
            while True:
                try:
                    # Todo: Send back to Twilio media stream
                    message = await websocket.recv()
                    data = json.loads(message)
                    if data.get("audio"):
                        yield base64.b64decode(data["audio"])
                    elif data.get('isFinal'):
                        break
                except websockets.exceptions.ConnectionClosed:
                    print("Connection closed")
                    break

        listen_task = asyncio.create_task(stream(listen(), socket, streamSid))

        text_buffer = ""

        print("Starting buffered chunking")
        async for text in text_iterator:
            if text is not None:
              text_buffer += text
              splitters = (".", ",", "? ", "! ", ";", ":", "(", ")", "[", "]", "}")
              if text_buffer.endswith(splitters):
                print(text_buffer);
                await websocket.send(json.dumps({"text": text_buffer, "try_trigger_generation": True}))
                text_buffer = ""

        await websocket.send(json.dumps({"text": ""}))

        await listen_task

async def stream(audio_stream, socket, streamSid):
    print("Streaming audio")
    """Stream audio data using mpv player."""

    audio = await audio_stream

    data = {}
    data['event'] = 'media'
    data['streamSid'] = streamSid
    data['payload'] = audio

    print(type(socket))
    print(data)
    socket.send(data);

    # if not is_installed("mpv"):
    #     raise ValueError(
    #         "mpv not found, necessary to stream audio. "
    #         "Install instructions: https://mpv.io/installation/"
    #     )

    # mpv_process = subprocess.Popen(
    #     ["mpv", "--no-cache", "--no-terminal", "--", "fd://0"],
    #     stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    # )

    # print("Started streaming audio")
    # async for chunk in audio_stream:
    #     if chunk:
    #         mpv_process.stdin.write(chunk)
    #         mpv_process.stdin.flush()

    # if mpv_process.stdin:
    #     mpv_process.stdin.close()
    # mpv_process.wait()