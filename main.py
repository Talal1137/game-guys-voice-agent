import os
import json
import uvicorn
import google.generativeai as genai
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from dotenv import load_dotenv
from datetime import datetime
import re

# Load environment variables from .env file
load_dotenv()

# --- Configuration ---
# Fix for PORT environment variable handling
port_env = os.getenv("PORT", "5050")
PORT = int(port_env) if port_env and port_env.strip() else 5050

DOMAIN = os.getenv("CLOUDFLARE_URL")
if not DOMAIN:
    raise ValueError("CLOUDFLARE_URL environment variable not set.")
WS_URL = f"wss://{DOMAIN}/ws"

# Updated greeting to reflect the new model
WELCOME_GREETING = "Hi, you've reached Game Guys support. I'm Jane. What seems to be the issue today?"

# System prompt for Gemini with Game Guys script
SYSTEM_PROMPT = """You are Jane, Game Guys' friendly voice assistant. You help customers with vending machine issues following a specific script.

IMPORTANT: DO NOT greet the customer again - they've already heard the welcome message. Jump straight into helping them.

CRITICAL RULES:
1. Keep replies very short (1-2 sentences max)
2. Ask only 1-2 questions at a time like a human would
3. Be warm, friendly, and professional - never use casual expressions like "Oh no", "Yikes", etc.
4. Follow the exact call flow and script provided
5. Spell out all numbers (e.g., say 'one thousand two hundred' instead of 1200)
6. No special characters, asterisks, bullet points, or emojis
7. Always collect information step by step, never all at once
8. If customer says goodbye, thank them and end professionally

CALL FLOW:
1. Customer has already heard the greeting, so start by understanding their issue
2. ALWAYS ASK LOCATION FIRST: "Which shopping centre is the machine in?"

3. ISSUE TYPES TO DETECT:
- Product stuck/not dispensed
- Wrong product dispensed  
- Payment issues (charged but no product, double charge)
- Card reader not working
- Touchscreen not responding
- Machine frozen mid-transaction
- Machine offline
- Door light on
- Door locked after timeout
- Lift status error

4. FOR REFUND ISSUES (product stuck + charged, wrong product, payment issues):
   - Collect in small chunks:
   - First: Amount charged and approximate time (ask for BOTH together if not provided)
   - Then: Payment method (physical card vs phone/watch)
   - If phone/watch: Give wallet instructions for last 4 digits
   - If physical card: Ask for last 4 digits of card
   - If product issue: Ask row number
   - Finally: Ask for one contact (phone or email)
   - Offer to text refund instructions
   - End with: "Thank you. I have all the details I need. If you need further assistance you can email us at info@gameguys.com.au."

5. FOR NON-REFUND ISSUES:
   - Give appropriate response from script
   - Keep very short
   - Escalate when needed
   - End with: "Thanks for letting us know - I've logged this for our team. Have a great day!"

WALLET INSTRUCTIONS (only if caller asks for help):
iPhone/Apple Watch:
      â€œOpen Wallet, select the card, tap the three dots,
       find Device Account Number, share the last four digits.â€
   Android/Google Wallet:
      â€œOpen Google Wallet, select the card, tap Card details,
       find Virtual Account Number, share the last four digits.â€

Remember: Always sound human. Confirm briefly after each detail, then move on."""

# --- Gemini API Initialization ---
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
if not GOOGLE_API_KEY:
    raise ValueError("GOOGLE_API_KEY environment variable not set.")

genai.configure(api_key=GOOGLE_API_KEY)

model = genai.GenerativeModel(
    model_name='gemini-2.5-flash',
    system_instruction=SYSTEM_PROMPT
)

# Store active chat sessions and call data
sessions = {}
call_data = {}

# Create FastAPI app
app = FastAPI()

@app.get("/")
def root():
    return {"message": "Game Guys Voice Assistant is running."}

def initialize_call_data(call_sid, caller_number=None):
    """Initialize data structure for a new call"""
    call_data[call_sid] = {
        "timestamp": datetime.now().isoformat(),
        "caller_number": caller_number or "Unknown",
        "call_sid": call_sid,
        "issue_type": "",
        "location": "",
        "row_number": "",
        "amount": "",
        "contact_info": "",
        "notes": "",
        "conversation_stage": "greeting",
        "payment_method": "",
        "transaction_time": "",
        "last_4_digits": "",
        "photo_mentioned": False,
        "call_ended": False,
        "pending_info": []  # Track what info we're waiting for
    }


def save_call_data_to_json(call_sid):
    """Save call data to JSON file"""
    if call_sid not in call_data:
        print(f"No call data found for {call_sid}")
        return

    try:
        # Create calls directory if it doesn't exist
        os.makedirs("call_logs", exist_ok=True)
        print(f"Created/verified call_logs directory")

        # Save individual call data
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"call_logs/call_{call_sid}_{timestamp}.json"
        
        print(f"Attempting to save to: {filename}")
        with open(filename, 'w') as f:
            json.dump(call_data[call_sid], f, indent=2)
        print(f"Individual call file saved: {filename}")

        # Also append to master log file
        master_log = "call_logs/master_call_log.json"
        master_data = []

        # Load existing master data if file exists
        if os.path.exists(master_log):
            try:
                with open(master_log, 'r') as f:
                    master_data = json.load(f)
                print(f"Loaded existing master log with {len(master_data)} entries")
            except Exception as e:
                print(f"Error reading master log: {e}")
                master_data = []

        # Add current call data
        master_data.append(call_data[call_sid])

        # Save updated master log
        with open(master_log, 'w') as f:
            json.dump(master_data, f, indent=2)
        print(f"Master log updated with {len(master_data)} total entries")

        print(f"Call data successfully saved for {call_sid}")
        
    except Exception as e:
        print(f"ERROR saving call data for {call_sid}: {e}")
        import traceback
        traceback.print_exc()


def update_call_data(call_sid, **kwargs):
    """Update call data with new information and save when important data is collected"""
    if call_sid in call_data:
        call_data[call_sid].update(kwargs)
        
        # Save immediately when we collect important information
        important_fields = ['issue_type', 'location', 'amount', 'transaction_time', 'payment_method', 'last_4_digits', 'contact_info', 'row_number']
        if any(field in kwargs for field in important_fields):
            try:
                save_call_data_to_json(call_sid)
                print(f"Auto-saved call data after collecting: {list(kwargs.keys())}")
            except Exception as e:
                print(f"Error auto-saving call data for {call_sid}: {e}")


async def gemini_response(chat_session, user_prompt, call_sid):
    """Get a response from the Gemini API and update call data"""

    # Check for goodbye/end call phrases first
    user_lower = user_prompt.lower()
    goodbye_phrases = ['goodbye', 'bye', 'thanks bye', 'thank you bye', "that's all", 'end call', 'hang up']
    if any(phrase in user_lower for phrase in goodbye_phrases):
        update_call_data(call_sid, call_ended=True)
        return "Thank you for calling Game Guys support. We'll take care of this for you. Have a great day!"

    # Add context about current call data to the prompt
    context = ""
    missing_info = []
    if call_sid in call_data:
        data = call_data[call_sid]
        context = f"\nCurrent call context: Issue={data['issue_type']}, Location='{data['location']}', Amount='{data['amount']}', Time='{data['transaction_time']}', Payment='{data['payment_method']}', Contact='{data['contact_info']}', Row='{data['row_number']}'"

        # Check what information is still missing for refund cases
        if data['issue_type'] in ['Product stuck', 'Payment issue', 'Wrong product']:
            if not data['location']:
                missing_info.append('location')
            if not data['amount']:
                missing_info.append('amount')
            if not data['transaction_time']:
                missing_info.append('time')
            if not data['payment_method']:
                missing_info.append('payment method')
            if data['payment_method'] and not data['last_4_digits']:
                missing_info.append('last 4 digits')
            if data['issue_type'] == 'Product stuck' and not data['row_number']:
                missing_info.append('row number')
            if not data['contact_info']:
                missing_info.append('contact info')

        if missing_info:
            context += f"\nSTILL NEED: {', '.join(missing_info)}. Focus on getting the missing information one at a time."
        elif data['issue_type'] in ['Product stuck', 'Payment issue', 'Wrong product'] and len(missing_info) == 0:
            # All refund information collected - provide ending instruction
            context += f"\nALL INFORMATION COLLECTED. End with: 'Please email info@gameguys.com.au with these details: {data['location']}, ${data['amount']}, {data['transaction_time']}, last 4 digits {data['last_4_digits']}, and a photo if possible. We'll process your refund quickly. Thanks for calling Game Guys - have a great day!'"

    full_prompt = f"{user_prompt}{context}"

    # Send prompt to Gemini/chat session
    response = await chat_session.send_message_async(full_prompt)
    response_text = getattr(response, 'text', str(response))

    # Update call data BEFORE processing - parse user input first
    if call_sid in call_data:
        response_lower = response_text.lower()

        # Detect issue types based on what user said
        if any(word in user_lower for word in ['stuck', 'not dispensed', "didnt come out", "didn't come out"]):
            update_call_data(call_sid, issue_type="Product stuck")
        elif any(word in user_lower for word in ['wrong product', 'different item', 'incorrect product']):
            update_call_data(call_sid, issue_type="Wrong product")
        elif any(word in user_lower for word in ['charged', 'payment', 'double charge', 'billed']):
            update_call_data(call_sid, issue_type="Payment issue")
        elif any(word in user_lower for word in ['card reader', 'tap not working']):
            update_call_data(call_sid, issue_type="Card reader")
        elif any(word in user_lower for word in ['touchscreen', 'screen not working']):
            update_call_data(call_sid, issue_type="Touchscreen")
        elif any(word in user_lower for word in ['frozen', 'stuck mid', 'stopped working']):
            update_call_data(call_sid, issue_type="Machine frozen")
        elif any(word in user_lower for word in ['offline', 'not responding']):
            update_call_data(call_sid, issue_type="Machine offline")
        elif any(word in user_lower for word in ['door light', 'light on']):
            update_call_data(call_sid, issue_type="Door light")
        elif any(word in user_lower for word in ['door locked', 'door stuck']):
            update_call_data(call_sid, issue_type="Door locked")
        elif any(word in user_lower for word in ['lift', 'lift status']):
            update_call_data(call_sid, issue_type="Lift status error")

        # Detect location mentions - improved detection with fuzzy matching
        shopping_centres_map = {
            'melbourne central': 'Melbourne Central',
            'melborne central': 'Melbourne Central',  # Common misspelling
            'melbourne center': 'Melbourne Central',   # Alternate spelling
            'melborne center': 'Melbourne Central',    # Common misspelling + alternate
            'westfield': 'Westfield',
            'chadstone': 'Chadstone',
            'chaddy': 'Chadstone',  # Common nickname
            'collins place': 'Collins Place',
            'emporium': 'Emporium',
            'bourke street': 'Bourke Street',
            'chapel street': 'Chapel Street'
        }
        
        for variant, correct_name in shopping_centres_map.items():
            if variant in user_lower:
                update_call_data(call_sid, location=correct_name)
                break

        # Detect amounts (improved pattern) - USING YOUR EXACT CODE
        amount_patterns = [
            r'\$(\d+(?:\.\d{2})?)',   # $10, $10.50
            r'(\d+)\s*dollars?',       # 10 dollars, 10 dollar
            r'charged.*?(\d+)',        # charged 10, charged about 10
            r'paid.*?(\d+)',           # paid 10, paid about 10
            r'cost.*?(\d+)',           # cost 10, cost me 10
            r'about\s+(\d+)',         # about 10
            r'around\s+(\d+)',        # around 10
        ]

        for pattern in amount_patterns:
            try:
                amount_match = re.search(pattern, user_lower)
            except re.error:
                continue
            if amount_match and not call_data[call_sid]['amount']:
                potential_amount = amount_match.group(1)
                # Accept decimal amounts as well
                try:
                    amt_val = float(potential_amount)
                except Exception:
                    continue
                if 0 < amt_val <= 10000:  # reasonable upper bound
                    # store as string to avoid formatting surprises
                    update_call_data(call_sid, amount=str(potential_amount))
                    break

        # Detect time mentions
        time_patterns = [
            r'(\d{1,2})\s*(?:pm|am)',      # 12pm, 12 pm
            r'noon',                        # noon
            r'midnight',                    # midnight
            r'(\d{1,2}):\d{2}\s*(?:pm|am)?', # 12:30, 12:30pm
            r'around\s+(\d{1,2})',         # around 12
            r'about\s+(\d{1,2})',          # about 12
            r'(\d{1,2})\s+(?:o\'clock|oclock)', # 12 o'clock
        ]

        for pattern in time_patterns:
            try:
                time_match = re.search(pattern, user_lower)
            except re.error:
                continue
            if time_match and not call_data[call_sid]['transaction_time']:
                match_text = time_match.group(0)
                if 'noon' in match_text:
                    update_call_data(call_sid, transaction_time="12:00 PM")
                elif 'midnight' in match_text:
                    update_call_data(call_sid, transaction_time="12:00 AM")
                else:
                    # prefer first capture group if present
                    if time_match.groups():
                        hour = time_match.group(1)
                        update_call_data(call_sid, transaction_time=hour)
                    else:
                        update_call_data(call_sid, transaction_time=match_text)
                break

        # Detect payment method
        if any(word in user_lower for word in ['physical card', 'card', 'credit card', 'debit card']) and 'phone' not in user_lower and 'watch' not in user_lower:
            update_call_data(call_sid, payment_method="physical_card")
        elif any(word in user_lower for word in ['phone', 'mobile', 'cellphone', 'iphone', 'android']):
            update_call_data(call_sid, payment_method="phone")
        elif any(word in user_lower for word in ['watch', 'apple watch', 'smartwatch']):
            update_call_data(call_sid, payment_method="watch")

        # Detect contact info (improved patterns)
        # Handle "use this number" or "the one I'm calling from"
        if any(phrase in user_lower for phrase in ['use this number', 'calling from', 'this number', 'same number', 'calling you with', 'phone number i\'m calling', 'number i\'m calling']) and not call_data[call_sid]['contact_info']:
            # Use the caller's number from call data
            if call_data[call_sid]['caller_number'] != "Unknown":
                update_call_data(call_sid, contact_info=call_data[call_sid]['caller_number'])
        else:
            # Standard email/phone detection
            email_match = re.search(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', user_prompt, re.IGNORECASE)
            # Handle spaced phone numbers like "0 3 3 3 3 3 3"
            spaced_phone = re.search(r'(\d)\s+(\d)\s+(\d)\s+(\d)\s+(\d)\s+(\d)\s+(\d)', user_prompt)
            
            if email_match and not call_data[call_sid]['contact_info']:
                update_call_data(call_sid, contact_info=email_match.group())
            elif spaced_phone and not call_data[call_sid]['contact_info']:
                # Combine spaced digits into phone number
                phone_number = ''.join(spaced_phone.groups())
                update_call_data(call_sid, contact_info=phone_number)
            elif not call_data[call_sid]['contact_info']:
                # Australian phone number patterns
                phone_patterns = [
                    r'\b(\d{4}\s?\d{3}\s?\d{3})\b',      # 0400 123 456
                    r'\b(\d{10})\b',                      # 0400123456
                    r'\b(\+61\s?\d{3}\s?\d{3}\s?\d{3})\b', # +61 400 123 456
                ]
                
                for pattern in phone_patterns:
                    phone_match = re.search(pattern, user_prompt)
                    if phone_match:
                        update_call_data(call_sid, contact_info=phone_match.group())
                        break

        # Detect "don't know" responses for various fields
        dont_know_phrases = ["don't know", "dont know", "not sure", "no idea", "can't remember", "cant remember", "unsure", "i don't know", "i dont know", "don't remember", "dont remember", "i don't remember", "i dont remember"]
        
        if any(phrase in user_lower for phrase in dont_know_phrases):
            # Check what information was being asked for based on context or recent assistant response
            if any(word in response_lower for word in ['row', 'number']) and not call_data[call_sid]['row_number']:
                update_call_data(call_sid, row_number="Customer didn't know")
            elif any(word in response_lower for word in ['amount', 'charged', 'cost']) and not call_data[call_sid]['amount']:
                update_call_data(call_sid, amount="Customer didn't know")
            elif any(word in response_lower for word in ['time', 'when']) and not call_data[call_sid]['transaction_time']:
                update_call_data(call_sid, transaction_time="Customer didn't know")
            elif any(word in response_lower for word in ['digits', 'card']) and not call_data[call_sid]['last_4_digits']:
                update_call_data(call_sid, last_4_digits="Customer didn't know")
        
        # Detect row numbers (improved) - only if not already marked as "don't know"
        if not call_data[call_sid]['row_number'] or call_data[call_sid]['row_number'] == "Customer didn't know":
            row_patterns = [
                r'\b([A-Z]\d+)\b',        # A1, B23
                r'\b(row\s*[A-Z]?\d+)\b', # row A1, row 23
                r'\b([A-Z]{1,2}\d+)\b',   # AB12
            ]
            
            for pattern in row_patterns:
                row_match = re.search(pattern, user_prompt, re.IGNORECASE)
                if row_match:
                    update_call_data(call_sid, row_number=row_match.group(1).upper())
                    break

        # Detect last 4 digits (improved)
        # Look for patterns like "4 4 6 6" or "4466" when digits are being asked for
        if not call_data[call_sid]['last_4_digits']:
            # Pattern for spaced digits like "4 4 6 6"
            spaced_digits = re.search(r'(\d)\s+(\d)\s+(\d)\s+(\d)', user_prompt)
            if spaced_digits:
                digits = ''.join(spaced_digits.groups())
                update_call_data(call_sid, last_4_digits=digits)
            else:
                # Standard patterns
                digits_patterns = [
                    r'(\d{4})',                           # Any 4 digits
                    r'last.*?(\d{4})',                   # last four digits 1234
                    r'digits.*?(\d{4})',                 # digits are 1234
                    r'card.*?(\d{4})',                   # card ending 1234
                ]
                
                if any(word in user_lower for word in ['last', 'digits', 'card']):
                    for pattern in digits_patterns:
                        digits_match = re.search(pattern, user_prompt)
                        if digits_match:
                            potential_digits = digits_match.group(1)
                            # Avoid capturing years, amounts, etc.
                            if potential_digits not in [call_data[call_sid]['amount'], call_data[call_sid]['transaction_time']]:
                                update_call_data(call_sid, last_4_digits=potential_digits)
                                break

        # Detect photo mentions
        if any(word in response_lower for word in ['photo', 'picture', 'video']):
            update_call_data(call_sid, photo_mentioned=True)

        # Update notes with key information
        notes = []
        if call_data[call_sid]['last_4_digits']:
            notes.append(f"Last 4 digits: {call_data[call_sid]['last_4_digits']}")
        if call_data[call_sid]['photo_mentioned']:
            notes.append("Photo/video mentioned")
        if 'wallet' in response_lower or 'device account' in response_lower:
            notes.append("Wallet instructions given")

        if notes:
            update_call_data(call_sid, notes="; ".join(notes))

        # Debug print to see what data was captured
        print(f"Updated call data for {call_sid}: {call_data[call_sid]}")

    return response_text


@app.post("/twiml")
async def twiml_endpoint():
    """Endpoint that returns TwiML for Twilio to connect to the WebSocket"""
    xml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
    <Connect>
    <ConversationRelay url="{WS_URL}" welcomeGreeting="{WELCOME_GREETING}" ttsProvider="Google" voice="en-AU-Wavenet-A" />
    </Connect>
    </Response>"""

    return Response(content=xml_response, media_type="text/xml")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time communication"""
    await websocket.accept()
    call_sid = None

    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)

            if message.get("type") == "setup":
                call_sid = message.get("callSid")
                caller_number = message.get("from", "Unknown")
                print(f"Setup for call: {call_sid} from {caller_number}")

                # Initialize call data and chat session
                initialize_call_data(call_sid, caller_number)
                sessions[call_sid] = model.start_chat(history=[])

            elif message.get("type") == "prompt":
                if not call_sid or call_sid not in sessions:
                    print(f"Error: Received prompt for unknown call_sid {call_sid}")
                    continue

                user_prompt = message.get("voicePrompt", "")
                print(f"Processing prompt: {user_prompt}")

                chat_session = sessions[call_sid]
                response_text = await gemini_response(chat_session, user_prompt, call_sid)

                # If the call was ended by the assistant, save data and send goodbye
                if call_sid in call_data and call_data[call_sid].get('call_ended', False):
                    # Save the call data BEFORE closing
                    try:
                        save_call_data_to_json(call_sid)
                        print(f"Successfully saved call data before goodbye for {call_sid}")
                    except Exception as e:
                        print(f"Error saving call data before goodbye for {call_sid}: {e}")
                    
                    await websocket.send_text(json.dumps({
                        "type": "text",
                        "token": response_text,
                        "last": True
                    }))
                    print(f"Sent goodbye response: {response_text}")
                    
                    # Clean up the data structures
                    if call_sid in call_data:
                        call_data.pop(call_sid, None)
                    if call_sid in sessions:
                        sessions.pop(call_sid, None)
                    
                    await websocket.close()
                    return

                await websocket.send_text(
                    json.dumps({
                        "type": "text",
                        "token": response_text,
                        "last": True
                    })
                )
                print(f"Sent response: {response_text}")

            elif message.get("type") == "interrupt":
                print(f"Handling interruption for call {call_sid}.")

            else:
                print(f"Unknown message type received: {message.get('type')}")

    except WebSocketDisconnect:
        print(f"WebSocket connection closed for call {call_sid}")

        # Save call data before cleaning up - ALWAYS save regardless of how call ended
        if call_sid and call_sid in call_data:
            try:
                save_call_data_to_json(call_sid)
                print(f"Successfully saved call data for {call_sid}")
            except Exception as e:
                print(f"Error saving call data for {call_sid}: {e}")
            finally:
                call_data.pop(call_sid, None)

        if call_sid and call_sid in sessions:
            sessions.pop(call_sid, None)

        print(f"Cleared session and saved data for call {call_sid}")


if __name__ == "__main__":
    print(f"Starting Game Guys Voice Assistant on port {PORT}")
    print(f"WebSocket URL for Twilio: {WS_URL}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
