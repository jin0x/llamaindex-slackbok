# read .env files
import dotenv, os
dotenv.load_dotenv()

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import re
import json
import requests
from pyboxen import boxen

# Bring in deps including Slack Bolt framework
from slack_bolt import App
from flask import Flask, request, jsonify
from slack_bolt.adapter.flask import SlackRequestHandler

# bring in llamaindex deps and initialize index
from llama_index.core import (
    VectorStoreIndex,
    Document,
    Settings,
    # SimpleDirectoryReader,
    StorageContext,
    load_index_from_storage,
)
from llama_index.llms.ollama import Ollama
from llama_index.core.node_parser import SentenceSplitter
from llama_index.readers.web import SimpleWebPageReader
from llama_index.llms.groq import Groq

text_splitter = SentenceSplitter(chunk_size=200, chunk_overlap=10)

Settings.text_splitter = text_splitter
Settings.llm = Ollama(model="llama3:latest")
Settings.llm = Groq(model="mixtral-8x7b-32768")

PERSIST_DIR = "./storage"
if not os.path.exists(PERSIST_DIR):
    # load the documents and create the index
    # documents = SimpleDirectoryReader('data').load_data()
    documents = SimpleWebPageReader(html_to_text=True).load_data(
        ['http://woocommerce.github.io/woocommerce-rest-api-docs/?shell#introduction']
    )
    index = VectorStoreIndex.from_documents(documents, show_progress=True, transformations=[text_splitter])
    index.storage_context.persist()
else:
    # load the existing index
    storage_context = StorageContext.from_defaults(persist_dir=PERSIST_DIR)
    index = load_index_from_storage(storage_context)

SYSTEM_PROMPT = (
    "As a WooCommerce AI Store Assistant, your role is to process requests related to store management "
    "tasks such as generating coupons or creating new products. For each user request, provide a response "
    "that includes the corresponding payload that needed in an object format and the full endpoint URL without the domain URL, only everything after and including /wp-json/wc.",
    "Your response should look exactly like this <PAYLOAD>Here you should put the payload object</PAYLOAD><ENDPOINT>Here you should put the endpoint</ENDPOINT>",
    "You should only include the payload and the endpoint inside the template tags",
    "If a request cannot be fulfilled because it is not supported by your current capabilities, instruct the user to modify their inquiry."
    "Always ensure your responses are actionable and precise."
)

# Initialize Bolt app with token and secret
app = App(
    token=os.environ.get("SLACK_BOT_TOKEN"),
    signing_secret=os.environ.get("SLACK_SIGNING_SECRET")
)
handler = SlackRequestHandler(app)

# start flask app
flask_app = Flask(__name__)

# join the #bot-testing channel so we can listen to messages
channel_list = app.client.conversations_list().data
channel = next((channel for channel in channel_list.get('channels') if channel.get("name") == "bot-testing"), None)
channel_id = channel.get('id')
app.client.conversations_join(channel=channel_id)
print(f"Found the channel {channel_id} and joined it")

# get the bot's own user ID so it can tell when somebody is mentioning it
auth_response = app.client.auth_test()
bot_user_id = auth_response["user_id"]

def boxen_print(*args, **kwargs):
    print(boxen(*args, **kwargs))


def dispatch_store_action(data):
    payload_match = re.search(r'<PAYLOAD>(.*?)</PAYLOAD>', data, re.DOTALL)
    endpoint_match = re.search(r'<ENDPOINT>(.*?)</ENDPOINT>', data, re.DOTALL)

    print(type(data))
    print(f"Payload Match: {payload_match}")
    print(f"Endpoint Match: {endpoint_match}")


    headers = {
        'Content-Type': 'application/json',
    }

    if payload_match and endpoint_match:
        payload = payload_match.group(1).strip()
        endpoint = endpoint_match.group(1).strip()
        full_url = f"{os.environ.get("WC_DOMAIN")}/{endpoint}"

        boxen_print(
            f"Payload: {payload}",
            title="Payload",
            color="yellow"
        )
        boxen_print(
            f"Payload: {endpoint}",
            title="Endpoint",
            color="yellow"
        )

        headers = {'Content-Type': 'application/json'}

        response = requests.post(
            full_url,
            headers=headers,
            json=json.loads(payload),
            auth=(os.environ.get("WC_CONSUMER_KEY"), os.environ.get("WC_CONSUMER_SECRET")),
            verify=False
        )

        if response.status_code == 200 or response.status_code == 201:
            response_text = f"API call successful!"
        else:
            response_text = f"Failed API call: {response.status_code} - {response.text}"

        # Response box
        boxen_print(
            response_text,
            title="API Response",
            color="green" if response.status_code == 200 or response.status_code == 201  else "red"
        )

    else:
        boxen_print("Failed to extract payload or endpoint.", title="Error", color="red")

    return response_text


# this is the challenge route required by Slack
# if it's not the challenge it's something for Bolt to handle
@flask_app.route("/", methods=["POST"])
def slack_challenge():
    if request.json and "challenge" in request.json:
        print("Received challenge")
        return jsonify({"challenge": request.json["challenge"]})
    else:
        print("Incoming event:")
        print(request.json)
    return handler.handle(request)

# this handles any incoming message the bot can hear
# we want it to only respond when somebody messages it directly
# otherwise it listens and stores every message as future context
@app.message()
def reply(message, say):
    # the slack message object is a complicated nested object
    # if message contains a "blocks" key
    #   then look for a "block" with the type "rich text"
    #       if you find it
    #       then look inside that block for an "elements" key
    #           if you find it
    #               then examine each one of those for an "elements" key
    #               if you find it
    #                   then look inside each "element" for one with type "user"
    #                   if you find it
    #                   and if that user matches the bot_user_id
    #                       then it's a message for the bot
    if message.get('blocks'):
        for block in message.get('blocks'):
            if block.get('type') == 'rich_text':
                for rich_text_section in block.get('elements'):
                    for element in rich_text_section.get('elements'):
                        if element.get('type') == 'user' and element.get('user_id') == bot_user_id:
                            for element in rich_text_section.get('elements'):
                                if element.get('type') == 'text':
                                    query = element.get('text')
                                    query_engine = index.as_query_engine()
                                    response = query_engine.query(f"{SYSTEM_PROMPT} {query}")
                                    # User query box
                                    boxen_print(
                                        f"Somebody asked the bot: {query}",
                                        title="User query",
                                        color="yellow"
                                    )
                                    # Context box
                                    boxen_print(
                                        f"Context was: {response.source_nodes}",
                                        title="Info",
                                        color="cyan"
                                    )
                                    # Response box
                                    boxen_print(
                                        f"Response was: {str(response)}",
                                        title="AI Bot",
                                        color="green"
                                    )

                                    response_text = dispatch_store_action(str(response))
                                    say(response_text)
                                    # say(str(response))
                                    return
    # otherwise treat it as a document to store
    index.insert(Document(text=message.get('text')))
    print("Stored message", message.get('text'))

if __name__ == "__main__":
    flask_app.run(port=3000)
