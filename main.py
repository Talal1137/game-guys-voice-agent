import os
import json
import uvicorn
import google.generativeai as genai
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from dotenv import load_dotenv
from datetime import datetime
import re
import base64
from google.oauth2 import service_account
from googleapiclient.discovery import build
import tempfile

# Load environment variables from .env file
load_dotenv()

# --- Configuration ---
PORT = int(os.getenv("PORT", "8080"))
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
9. If customer makes a correction to previous information, acknowledge it briefly and update accordingly
10. If customer says just "sorry" or "pardon", repeat your last question
11. NEVER say "Thank you for calling Game Guys" - the customer has already heard the welcome greeting

CALL FLOW:
1. Customer has already heard the greeting, so start by understanding their issue
2. ALWAYS ASK LOCATION FIRST: "What is the location of the machine?"

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
- Card stuck in machine (special case)

4. FOR REFUND ISSUES (product stuck + charged, wrong product, payment issues):
   - Collect in small chunks:
   - First: Amount charged and approximate time (ask for BOTH together if not provided)
   - Then: Payment method (physical card vs phone/watch)
   - If phone/watch: Give wallet instructions for last 4 digits
   - If physical card: Ask for last 4 digits of card
   - If product issue: Ask what product and row number
   - Finally: Ask for one contact (phone or email)
   - End with: "Thank you for the information. We'll process your refund and you should see it within three to five business days. It would be helpful if you could send a photo or video of the issue to info@gameguys.com.au. Thanks for calling Game Guys and goodbye!"

5. FOR CARD STUCK ISSUES:
   - Follow same data collection
   - End with: "Thank you for the information. We'll arrange for our technician to retrieve your card and process any refund needed. This will be resolved within twenty four hours. It would be helpful if you could send a photo or video of the stuck card to info@gameguys.com.au. Thanks for calling Game Guys and goodbye!"

6. FOR NON-REFUND ISSUES:
   - Give appropriate response from script
   - Keep very short
   - Escalate when needed
   - End with: "Thanks for letting us know - I've logged this for our team to investigate. If you have any questions, please email us at info@gameguys.com.au. Thanks for calling Game Guys and goodbye!"

WALLET INSTRUCTIONS:
For iPhone/Apple Watch: "Open Wallet, select the card, tap the three dots, find Device Account Number, give me last 4 digits"
For Android/Google Wallet: "Open Google Wallet, select card, tap Card details, find Virtual Account Number, give me last 4 digits"

Remember: Be human-like, ask one thing at a time, confirm briefly, then move to next step."""

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

# --- Google Sheets Setup ---
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
GOOGLE_CREDENTIALS_B64 = os.getenv("GOOGLE_CREDENTIALS_B64")

# Valid vending machine locations
VALID_LOCATIONS = [
    "Castle Hill", "Casula Mall", "Eastern Creek Quarter", "ECQ", "Ed Square", "Edmondson Park",
    "Macquarie Centre", "Marrickville Metro", "Mounties", "Parramatta Westfields", 
    "Rouse Hill Shopping Centre", "Top Ryde", "Oasis", "World Square", "Pacific Fair", "PAC Fair",
    "Ashfield Mall", "Westpoint", "The Grove Shopping Centre", "Castle Hill Towers",
    "Burwood Chinatown", "Showground Village", "Central Park Mall", "Macarthur Square",
    "Bankstown Central", "Broadway Shopping Centre", "Carlingford Court", 
    "East Village Shopping Centre", "Winston Hills", "Roselands Shopping Centre",
    "Merrylands Stocklands", "Wetherill Park Stocklands", "Bass Hill Plaza", "North Rocks", "Southgate",
    "Birkenhead Point"
]

# Master JSON file for call logs
MASTER_JSON_FILE = "master_call_log.json"

def get_sheets_service():
    """Initialize Google Sheets service"""
    if not GOOGLE_CREDENTIALS_B64:
        return None
    try:
        creds_json = base64.b64decode(GOOGLE_CREDENTIALS_B64).decode()
        creds_dict = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        return build("sheets", "v4", credentials=creds)
    except Exception as e:
        print(f"Error initializing Google Sheets service: {e}")
        return None

def update_google_sheet(call_sid):
    """Update Google Sheet with call data"""
    if call_sid not in call_data or not SPREADSHEET_ID:
        return

    service = get_sheets_service()
    if not service:
        return

    try:
        data = call_data[call_sid]
        
        # Prepare row data with "Customer doesn't know" for missing fields
        row_values = [
            data.get("timestamp", ""),
            data.get("caller_number", ""),
            data.get("call_sid", ""),
            data.get("issue_type", "Customer doesn't know") if not data.get("issue_type") else data.get("issue_type"),
            data.get("location", "Customer doesn't know") if not data.get("location") else data.get("location"),
            data.get("row_number", "Customer doesn't know") if not data.get("row_number") else data.get("row_number"),
            data.get("product_name", "Customer doesn't know") if not data.get("product_name") else data.get("product_name"),
            data.get("amount", "Customer doesn't know") if not data.get("amount") else data.get("amount"),
            data.get("transaction_time", "Customer doesn't know") if not data.get("transaction_time") else data.get("transaction_time"),
            data.get("payment_method", "Customer doesn't know") if not data.get("payment_method") else data.get("payment_method"),
            data.get("last_4_digits", "Customer doesn't know") if not data.get("last_4_digits") else data.get("last_4_digits"),
            data.get("contact_info", "Customer doesn't know") if not data.get("contact_info") else data.get("contact_info"),
            data.get("notes", ""),
            str(data.get("photo_mentioned", False)),
            str(data.get("call_ended", False))
        ]

        service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Sheet1!A:O",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row_values]}
        ).execute()
        
        print(f"Successfully updated Google Sheet for call {call_sid}")
        
    except Exception as e:
        print(f"Error updating Google Sheet for {call_sid}: {e}")

def save_call_data_to_json(call_sid):
    """Save or update call_data in a single master JSON file"""
    if call_sid not in call_data:
        print(f"No call data found for {call_sid}")
        return

    try:
        # Get the call data
        entry = dict(call_data[call_sid])
        
        # Normalize identifier fields
        entry["call_sid"] = call_sid
        entry["call_id"] = call_sid

        # Load existing master log
        log_data = []
        if os.path.exists(MASTER_JSON_FILE):
            try:
                with open(MASTER_JSON_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        log_data = [loaded]
                    elif isinstance(loaded, list):
                        log_data = loaded
                    else:
                        print(f"[JSON] Warning: unexpected master log format. Recreating.")
                        log_data = []
            except (json.JSONDecodeError, ValueError) as e:
                print(f"[JSON] Warning: corrupted master log. Recreating. Error: {e}")
                log_data = []

        # Find existing entry
        existing_entry = None
        for item in log_data:
            if item.get("call_sid") == call_sid or item.get("call_id") == call_sid:
                existing_entry = item
                break

        if existing_entry:
            existing_entry.update(entry)
        else:
            log_data.append(entry)

        # Save atomically
        dirpath = os.path.dirname(os.path.abspath(MASTER_JSON_FILE)) or "."
        fd, tmp_path = tempfile.mkstemp(dir=dirpath, prefix="tmp_master_", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmpf:
                json.dump(log_data, tmpf, indent=2, ensure_ascii=False)
                tmpf.flush()
                os.fsync(tmpf.fileno())
            os.replace(tmp_path, MASTER_JSON_FILE)
            print(f"[JSON] Master log updated for call {call_sid}")
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except:
                    pass

    except Exception as e:
        print(f"[JSON] ERROR writing master log for call {call_sid}: {e}")

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
        "pending_info": [],
        "product_name": "",
        "card_stuck": False,
        "last_question": "",
        "conversation_history": [],
        "field_history": {}
    }

def update_call_data(call_sid, **kwargs):
    """Update call data with new information and save when important data is collected"""
    if call_sid in call_data:
        call_data[call_sid].update(kwargs)
        
        # Save immediately when we collect important information
        important_fields = ['issue_type', 'location', 'amount', 'transaction_time', 'payment_method', 'last_4_digits', 'contact_info', 'row_number', 'product_name']
        if any(field in kwargs for field in important_fields):
            try:
                save_call_data_to_json(call_sid)
                print(f"Auto-saved call data after collecting: {list(kwargs.keys())}")
            except Exception as e:
                print(f"Error auto-saving call data for {call_sid}: {e}")

def find_matching_location(user_input):
    """Find the best matching location from the valid locations list"""
    user_lower = user_input.lower()
    
    # Handle spelled-out locations first
    spelled_out_mappings = {
        'b u r w o o d': 'Burwood Chinatown',
        'b u r w o 0 d': 'Burwood Chinatown',  # Handle 0 as O
        'burwood': 'Burwood Chinatown',
        's n b u r w o o d': 'Burwood Chinatown',
        's n b u r w o 0 d': 'Burwood Chinatown',
    }
    
    # Clean up spacing and check spelled-out locations
    cleaned_input = re.sub(r'\s+', ' ', user_lower.strip())
    for spelled, location in spelled_out_mappings.items():
        if spelled in cleaned_input:
            return location
    
    # Direct matches second
    for location in VALID_LOCATIONS:
        if location.lower() in user_lower:
            return location
    
    # Handle special cases and common variations + speech recognition errors
    location_mappings = {
        # Original mappings
        'ecq': 'Eastern Creek Quarter',
        'eastern creek': 'Eastern Creek Quarter',
        'pac fair': 'Pacific Fair',
        'pacific fair': 'Pacific Fair',
        'edmondson': 'Ed Square',
        'ed square': 'Ed Square',
        'macquarie': 'Macquarie Centre',
        'parramatta': 'Parramatta Westfields',
        'westfield parramatta': 'Parramatta Westfields',
        'rouse hill': 'Rouse Hill Shopping Centre',
        'castle hill tower': 'Castle Hill Towers',
        'castle hill': 'Castle Hill',
        'merrylands': 'Merrylands Stocklands',
        'wetherill': 'Wetherill Park Stocklands',
        'wetherill park': 'Wetherill Park Stocklands',
        'bass hill': 'Bass Hill Plaza',
        'roselands': 'Roselands Shopping Centre',
        'broadway': 'Broadway Shopping Centre',
        'carlingford': 'Carlingford Court',
        'east village': 'East Village Shopping Centre',
        'central park': 'Central Park Mall',
        'macarthur': 'Macarthur Square',
        'bankstown': 'Bankstown Central',
        'ashfield': 'Ashfield Mall',
        'grove': 'The Grove Shopping Centre',
        'showground': 'Showground Village',
        'burwood': 'Burwood Chinatown',
        'marrickville': 'Marrickville Metro',
        'birkenhead': 'Birkenhead Point',
        
        # Speech recognition error mappings
        'ralph hill': 'Rouse Hill Shopping Centre',
        'rose hill': 'Rouse Hill Shopping Centre',
        'house hill': 'Rouse Hill Shopping Centre',
        'rousse hill': 'Rouse Hill Shopping Centre',
        'castle hill tower': 'Castle Hill Towers',
        'castle towers': 'Castle Hill Towers',
        'casula': 'Casula Mall',
        'casual mall': 'Casula Mall',
        'casual': 'Casula Mall',
        'eastern creek': 'Eastern Creek Quarter',
        'eastern creek quarter': 'Eastern Creek Quarter',
        'eastern creek shopping': 'Eastern Creek Quarter',
        'ed square': 'Ed Square',
        'edmondson': 'Ed Square',
        'edmundson': 'Ed Square',
        'edmondson park': 'Ed Square',
        'macquarie center': 'Macquarie Centre',
        'macquarie centre': 'Macquarie Centre',
        'macquarie': 'Macquarie Centre',
        'marrickville': 'Marrickville Metro',
        'marrickville metro': 'Marrickville Metro',
        'mountainies': 'Mounties',
        'mounty': 'Mounties',
        'parramatta westfield': 'Parramatta Westfields',
        'parramatta': 'Parramatta Westfields',
        'westfield parramatta': 'Parramatta Westfields',
        'top ride': 'Top Ryde',
        'top ryde': 'Top Ryde',
        'oasis': 'Oasis',
        'world square': 'World Square',
        'pacific fair': 'Pacific Fair',
        'pac fair': 'Pacific Fair',
        'ashfield': 'Ashfield Mall',
        'westpoint': 'Westpoint',
        'west point': 'Westpoint',
        'grove shopping': 'The Grove Shopping Centre',
        'grove': 'The Grove Shopping Centre',
        'the grove': 'The Grove Shopping Centre',
        'burwood chinatown': 'Burwood Chinatown',
        'chinatown': 'Burwood Chinatown',
        'showground village': 'Showground Village',
        'showgrounds': 'Showground Village',
        'central park': 'Central Park Mall',
        'macarthur': 'Macarthur Square',
        'mcarthur': 'Macarthur Square',
        'bankstown': 'Bankstown Central',
        'broadway': 'Broadway Shopping Centre',
        'broadway shopping': 'Broadway Shopping Centre',
        'carlingford': 'Carlingford Court',
        'east village': 'East Village Shopping Centre',
        'winston hills': 'Winston Hills',
        'winston hill': 'Winston Hills',
        'roselands': 'Roselands Shopping Centre',
        'rose lands': 'Roselands Shopping Centre',
        'merrylands stockland': 'Merrylands Stocklands',
        'merrylands': 'Merrylands Stocklands',
        'stockland merrylands': 'Merrylands Stocklands',
        'wetherill park': 'Wetherill Park Stocklands',
        'wetherill': 'Wetherill Park Stocklands',
        'weather hill': 'Wetherill Park Stocklands',
        'stockland wetherill': 'Wetherill Park Stocklands',
        'bass hill': 'Bass Hill Plaza',
        'base hill': 'Bass Hill Plaza',
        'north rocks': 'North Rocks',
        'north rock': 'North Rocks',
        'southgate': 'Southgate',
        'south gate': 'Southgate',
        'birkenhead': 'Birkenhead Point',
        'birken head': 'Birkenhead Point',
        'burkenhead': 'Birkenhead Point'
    }
    
    for key, location in location_mappings.items():
        if key in user_lower:
            return location
    
    return None

def detect_correction_patterns(user_input):
    """Detect if user is making a correction to previous information"""
    user_lower = user_input.lower()
    
    # Strong correction indicators
    strong_corrections = [
        r'(?:oh\s+)?(?:no\s+)?(?:sorry\s+)?(?:actually\s+)?(?:i\s+meant\s+)?(?:it\s+was\s+)(.+)',
        r'(?:sorry\s+)?(?:i\s+said\s+)?(?:the\s+wrong\s+)?(?:thing\s+)?(?:it\'s\s+actually\s+)(.+)',
        r'(?:correction\s+)?(?:i\s+mean\s+)(.+)',
        r'(?:wait\s+)?(?:no\s+)?(?:that\'s\s+wrong\s+)?(?:it\'s\s+)(.+)',
        r'(?:let\s+me\s+correct\s+that\s+)?(?:it\s+should\s+be\s+)(.+)',
        r'(?:i\s+made\s+a\s+mistake\s+)?(?:it\s+was\s+actually\s+)(.+)'
    ]
    
    # Check for correction keywords first
    correction_keywords = [
        'sorry', 'actually', 'i meant', 'correction', 'oh no', 'wait no', 
        "that's wrong", 'i made a mistake', 'let me correct', 'i said wrong'
    ]
    
    has_correction_keyword = any(keyword in user_lower for keyword in correction_keywords)
    
    if has_correction_keyword:
        # Try to extract the corrected information
        for pattern in strong_corrections:
            try:
                match = re.search(pattern, user_lower)
                if match and match.group(1).strip():
                    corrected_value = match.group(1).strip()
                    if len(corrected_value) > 1 and corrected_value not in ['it', 'that', 'this', 'the']:
                        return True, corrected_value
            except:
                continue
    
    return False, None

def detect_product_name(user_input):
    """Detect product names in user input"""
    product_patterns = [
        r'(?:it was|product was|item was|bought|purchased|wanted|tried to get|trying to buy)\s+(?:a\s+|an\s+|the\s+)?(.+?)(?:\s+but|\s+and|\s*$)',
        r'(?:a\s+|an\s+|the\s+)?(.+?)(?:\s+got stuck|\s+didn\'t come out|\s+was stuck)',
        r'(?:from\s+row\s+\w+\s+)(?:it was|was)\s+(?:a\s+|an\s+|the\s+)?(.+)',
        r'(?:the\s+product\s+was\s+)?(?:a\s+|an\s+|the\s+)?(.+?)(?:\s*$)',
    ]
    
    for pattern in product_patterns:
        try:
            match = re.search(pattern, user_input.lower())
            if match:
                product = match.group(1).strip()
                exclude_words = ['something', 'nothing', 'anything', 'the', 'a', 'an', 'item', 'product', 'thing', 'stuff']
                if product not in exclude_words and len(product) > 2:
                    return product
        except:
            continue
    
    return None

def handle_apology_or_confusion(user_input, call_sid):
    """Handle when customer says sorry or seems confused"""
    user_lower = user_input.lower().strip()
    
    simple_apologies = ['sorry', 'sorry?', 'pardon', 'pardon?', 'what', 'what?', 'huh', 'huh?', 'excuse me', 'can you repeat that']
    
    if user_lower in simple_apologies:
        if call_sid in call_data and call_data[call_sid].get('last_question'):
            return True, call_data[call_sid]['last_question']
    
    return False, None

def determine_correction_field(corrected_value, call_sid):
    """Determine which field the correction applies to based on context and content"""
    if call_sid not in call_data:
        return None, None
    
    data = call_data[call_sid]
    
    # Check if it's a location
    matched_location = find_matching_location(corrected_value)
    if matched_location:
        return 'location', matched_location
    
    # Check if it's a product
    food_keywords = [
        'chips', 'chocolate', 'candy', 'drink', 'coke', 'pepsi', 'water', 'juice',
        'sandwich', 'bread', 'cookie', 'biscuit', 'gum', 'mint', 'snack', 'bar',
        'nuts', 'crackers', 'soda', 'energy drink', 'coffee', 'tea', 'milk'
    ]
    if any(keyword in corrected_value.lower() for keyword in food_keywords):
        return 'product_name', corrected_value
    
    # Check if it's an amount
    amount_match = re.search(r'(\d+(?:\.\d{2})?)', corrected_value)
    if amount_match:
        amount_val = float(amount_match.group(1))
        if 0 < amount_val <= 1000:
            return 'amount', amount_match.group(1)
    
    # Check if it's a row number
    row_match = re.search(r'([A-Z]\d+)', corrected_value.upper())
    if row_match:
        return 'row_number', row_match.group(1)
    
    # Check if it's last 4 digits
    digits_match = re.search(r'(\d{4})', corrected_value)
    if digits_match and len(corrected_value.strip()) <= 10:
        return 'last_4_digits', digits_match.group(1)
    
    # Check if it's contact info
    if '@' in corrected_value:
        return 'contact_info', corrected_value
    phone_match = re.search(r'(\d{10}|\d{4}\s?\d{3}\s?\d{3})', corrected_value)
    if phone_match:
        return 'contact_info', corrected_value
    
    # Default fallback
    return 'product_name', corrected_value

async def gemini_response(chat_session, user_prompt, call_sid):
    """Get a response from the Gemini API and update call data"""

    # Check for goodbye/end call phrases first
    user_lower = user_prompt.lower()
    goodbye_phrases = ['goodbye', 'bye', 'thanks bye', 'thank you bye', "that's all", 'end call', 'hang up']
    if any(phrase in user_lower for phrase in goodbye_phrases):
        update_call_data(call_sid, call_ended=True)
        return "Thank you for calling Game Guys support. We'll take care of this for you. Have a great day!"

    # Handle apologies/confusion - check if customer wants question repeated
    is_simple_apology, repeated_question = handle_apology_or_confusion(user_prompt, call_sid)
    if is_simple_apology:
        return repeated_question

    # Check for corrections
    is_correction, corrected_value = detect_correction_patterns(user_prompt)
    
    # Build context for internal logic
    context_info = {}
    missing_info = []
    if call_sid in call_data:
        data = call_data[call_sid]
        context_info = {
            'issue': data['issue_type'],
            'location': data['location'],
            'amount': data['amount'],
            'time': data['transaction_time'],
            'payment': data['payment_method'],
            'product': data['product_name'],
            'contact': data['contact_info'],
            'row': data['row_number'],
            'card_stuck': data.get('card_stuck', False)
        }

        # Prepare prompt for Gemini with just essential context
        gemini_context = ""
        if is_correction:
            gemini_context = f"\nIMPORTANT: Customer is making a correction. The corrected information is: '{corrected_value}'. Acknowledge the correction briefly like 'Got it' or 'Thanks for the correction' then continue normally."

        # Check what information is still missing for refund cases
        refund_issues = ['Product stuck', 'Payment issue', 'Wrong product']
        if data['issue_type'] in refund_issues or data.get('card_stuck'):
            if not data['location']:
                missing_info.append('location')
                # Check if we need to ask for location clarification
                location_attempts = data.get('location_attempts', 0)
                if 0 < location_attempts < 3:
                    gemini_context += f"\nLocation not recognized (attempt {location_attempts}/3). Ask: 'I didn't catch that location name. Could you please repeat the name of the shopping centre or location?'"
            if not data['amount']:
                missing_info.append('amount')
            if not data['transaction_time']:
                missing_info.append('time')
            # Skip payment method question if card is stuck (we already know it's physical card)
            if not data['payment_method'] and not data.get('card_stuck'):
                missing_info.append('payment method')
            if data['payment_method'] and not data['last_4_digits']:
                missing_info.append('last 4 digits')
            if data['issue_type'] == 'Product stuck' and not data['row_number'] and not data.get('card_stuck'):
                missing_info.append('row number')
            if data['issue_type'] in ['Product stuck', 'Wrong product'] and not data['product_name'] and not data.get('card_stuck'):
                missing_info.append('product name')
            if not data['contact_info']:
                missing_info.append('contact info')

        if missing_info:
            gemini_context += f"\nSTILL NEED: {', '.join(missing_info)}. Focus on getting the missing information one at a time."
        elif data['issue_type'] in refund_issues or data.get('card_stuck'):
            if len(missing_info) == 0:
                # Fill in missing fields with "Customer doesn't know" for card stuck cases without charges
                if data.get('card_stuck') and not data.get('amount'):
                    update_call_data(call_sid, amount="Not charged")
                if data.get('card_stuck') and not data.get('transaction_time'):
                    update_call_data(call_sid, transaction_time="Customer doesn't know")
                if data.get('card_stuck') and not data.get('last_4_digits'):
                    update_call_data(call_sid, last_4_digits="Customer doesn't know")
                if data.get('card_stuck') and not data.get('row_number'):
                    update_call_data(call_sid, row_number="Customer doesn't know")
                if data.get('card_stuck') and not data.get('product_name'):
                    update_call_data(call_sid, product_name="Customer doesn't know")
                    
                # All information collected - provide ending instruction
                if data.get('card_stuck'):
                    gemini_context += f"\nALL INFORMATION COLLECTED. End with: 'Thank you for the information. We'll arrange for our technician to retrieve your card and process any refund needed. This will be resolved within twenty four hours. It would be helpful if you could send a photo or video of the stuck card to info@gameguys.com.au. Thanks for calling Game Guys and goodbye!'"
                else:
                    gemini_context += f"\nALL INFORMATION COLLECTED. End with: 'Thank you for the information. We'll process your refund and you should see it within three to five business days. It would be helpful if you could send a photo or video of the issue to info@gameguys.com.au. Thanks for calling Game Guys and goodbye!'"

    # Send only clean prompt to Gemini
    if gemini_context:
        full_prompt = f"{user_prompt}{gemini_context}"
    else:
        full_prompt = user_prompt

    # Send prompt to Gemini/chat session
    response = await chat_session.send_message_async(full_prompt)
    response_text = getattr(response, 'text', str(response))

    # Store the assistant's question for potential repetition
    if '?' in response_text and call_sid in call_data:
        questions = [q.strip() + '?' for q in response_text.split('?') if q.strip()]
        if questions:
            update_call_data(call_sid, last_question=questions[-1])

    # Update call data BEFORE processing - parse user input first
    if call_sid in call_data:
        response_lower = response_text.lower()

        # Handle corrections
        if is_correction and corrected_value:
            correction_field, final_value = determine_correction_field(corrected_value, call_sid)
            if correction_field and final_value:
                update_call_data(call_sid, **{correction_field: final_value})
                print(f"Correction applied: {correction_field} = {final_value}")

        # Detect card stuck special case
        card_stuck_phrases = [
            'card stuck', 'card got stuck', 'card was stuck', 'card is stuck',
            'card got trapped', 'card trapped', 'card stuck in', 'my card is stuck'
        ]
        if any(phrase in user_lower for phrase in card_stuck_phrases):
            update_call_data(call_sid, issue_type="Product stuck", card_stuck=True)
            if not call_data[call_sid].get('payment_method'):
                update_call_data(call_sid, payment_method="physical_card")

        # Detect issue types based on what user said
        elif not call_data[call_sid].get('issue_type'):
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

        # Detect location mentions with improved matching and retry logic
        if not is_correction and not call_data[call_sid].get('location'):
            matched_location = find_matching_location(user_prompt)
            if matched_location:
                update_call_data(call_sid, location=matched_location, location_attempts=0)
            else:
                # Location not recognized - check if we should ask for clarification
                current_attempts = call_data[call_sid].get('location_attempts', 0)
                if current_attempts < 3:
                    # Save the attempted location and increment counter
                    update_call_data(call_sid, location_attempts=current_attempts + 1)
                    # Will be handled by Gemini context to ask for clarification
                else:
                    # After 3 attempts, save whatever they said as location ONLY if we're currently asking for location
                    # Check if the last question was about location
                    last_question = call_data[call_sid].get('last_question', '').lower()
                    if 'location' in last_question or 'where' in last_question:
                        location_from_input = user_prompt.strip()
                        # Clean it up a bit
                        location_patterns = [
                            r'(?:machine is (?:in|at)\s+)(.+)',
                            r'(?:location is\s+)(.+)',
                            r'(?:it\'?s (?:in|at)\s+)(.+)',
                            r'(.+)'  # fallback - use the whole input
                        ]
                        
                        for pattern in location_patterns:
                            match = re.search(pattern, location_from_input, re.IGNORECASE)
                            if match:
                                cleaned_location = match.group(1).strip().title()
                                update_call_data(call_sid, location=cleaned_location, location_attempts=3)
                                break

        # Detect product name (but not for card stuck scenarios)
        if not is_correction and not call_data[call_sid].get('card_stuck'):
            detected_product = detect_product_name(user_prompt)
            # Update product name if we detect a better one, or if current one is generic
            current_product = call_data[call_sid].get('product_name', '')
            generic_products = ["i didn't get my product.", "product", "item", "thing"]
            
            if detected_product and detected_product not in ['my card', 'card', 'the card']:
                # Always update if current is generic or empty
                if not current_product or any(generic in current_product.lower() for generic in generic_products):
                    update_call_data(call_sid, product_name=detected_product)
                # Or if we detect a specific product name in response to product question
                elif any(word in call_data[call_sid].get('last_question', '').lower() for word in ['product', 'item', 'what']):
                    update_call_data(call_sid, product_name=detected_product)

        # Detect amounts (improved pattern)
        if not is_correction and not call_data[call_sid].get('amount'):
            # Check for "not charged" or "wasn't charged" first
            if any(phrase in user_lower for phrase in ["wasn't charged", "not charged", "didnt charge", "didn't charge", "no charge"]):
                update_call_data(call_sid, amount="Not charged")
            else:
                amount_patterns = [
                    r'\$(\d+(?:\.\d{2})?)',   # $10, $10.50
                    r'(\d+(?:\.\d{2})?)\s*dollars?',       # 10 dollars, 10 dollar
                    r'charged.*?(\d+(?:\.\d{2})?)',        # charged 10, charged about 10.50
                    r'paid.*?(\d+(?:\.\d{2})?)',           # paid 10, paid about 10.50
                    r'cost.*?(\d+(?:\.\d{2})?)',           # cost 10, cost me 10.50
                    r'about\s+(\d+(?:\.\d{2})?)',         # about 10, about 10.50
                    r'around\s+(\d+(?:\.\d{2})?)',        # around 10, around 10.50
                ]

                for pattern in amount_patterns:
                    try:
                        amount_match = re.search(pattern, user_lower)
                        if amount_match:
                            potential_amount = amount_match.group(1)
                            try:
                                amt_val = float(potential_amount)
                                if 0 < amt_val <= 10000:  # reasonable upper bound
                                    update_call_data(call_sid, amount=str(potential_amount))
                                    break
                            except ValueError:
                                continue
                    except re.error:
                        continue

        # Detect time mentions
        if not is_correction and not call_data[call_sid].get('transaction_time'):
            # Relative time patterns
            relative_patterns = {
                r'half\s+an?\s+hour\s+ago': '30 minutes ago',
                r'an?\s+hour\s+ago': '1 hour ago',
                r'(\d+)\s+hours?\s+ago': lambda m: f"{m.group(1)} hours ago",
                r'(\d+)\s+minutes?\s+ago': lambda m: f"{m.group(1)} minutes ago",
                r'just\s+now': 'Just now',
                r'this\s+morning': 'This morning',
                r'this\s+afternoon': 'This afternoon',
                r'this\s+evening': 'This evening'
            }

            for pattern, replacement in relative_patterns.items():
                match = re.search(pattern, user_lower)
                if match:
                    value = replacement(match) if callable(replacement) else replacement
                    update_call_data(call_sid, transaction_time=value)
                    break
            if not call_data[call_sid].get('transaction_time'):
                time_patterns = [
                    r'(\d{1,2}:\d{2}\s*(?:am|pm)?)',
                    r'(\d{1,2})\s*(?:pm|am)',
                    r'noon',
                    r'midnight',
                ]
                for pattern in time_patterns:
                    time_match = re.search(pattern, user_lower)
                    if time_match:
                        if 'noon' in pattern:
                            update_call_data(call_sid, transaction_time="12:00 PM")
                        elif 'midnight' in pattern:
                            update_call_data(call_sid, transaction_time="12:00 AM")
                        else:
                            update_call_data(call_sid, transaction_time=time_match.group(0))
                        break

        # Detect payment method
        if not is_correction and not call_data[call_sid].get('payment_method'):
            if any(word in user_lower for word in ['physical card', 'card', 'credit card', 'debit card']) and 'phone' not in user_lower and 'watch' not in user_lower:
                update_call_data(call_sid, payment_method="physical_card")
            elif any(word in user_lower for word in ['phone', 'mobile', 'cellphone', 'iphone', 'android']):
                update_call_data(call_sid, payment_method="phone")
            elif any(word in user_lower for word in ['watch', 'apple watch', 'smartwatch']):
                update_call_data(call_sid, payment_method="watch")
            elif any(word in user_lower for word in ['cash', 'coins', 'notes']):
                update_call_data(call_sid, payment_method="cash")

        # Detect contact info
        if not is_correction and not call_data[call_sid].get('contact_info'):
            if any(phrase in user_lower for phrase in ['use this number', 'calling from', 'this number', 'same number']):
                if call_data[call_sid]['caller_number'] != "Unknown":
                    update_call_data(call_sid, contact_info=call_data[call_sid]['caller_number'])
            else:
                email_match = re.search(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', user_prompt)
                spaced_phone = re.search(r'(\d\s+){6,}\d', user_prompt)
                
                # Handle spoken phone formats like "I for 1 5, double 8, 1 5 6 8"
                spoken_phone_patterns = [
                    r'(?:i\s*)?(?:for\s+)?(\d)\s*(\d)[,\s]*(?:double\s+)?(\d+)[,\s]*(\d)[,\s]*(\d)[,\s]*(\d)[,\s]*(\d)',
                    r'(\d)\s*(\d)\s*(\d)\s*(\d)[,\s]*(\d)\s*(\d)\s*(\d)\s*(\d)[,\s]*(\d)\s*(\d)',
                ]
                
                if email_match:
                    update_call_data(call_sid, contact_info=email_match.group())
                elif spaced_phone:
                    phone_number = ''.join(spaced_phone.group().split())
                    update_call_data(call_sid, contact_info=phone_number)
                else:
                    # Try spoken phone patterns
                    for pattern in spoken_phone_patterns:
                        spoken_match = re.search(pattern, user_prompt, re.IGNORECASE)
                        if spoken_match:
                            # Extract and clean up the phone number
                            groups = [g for g in spoken_match.groups() if g]
                            # Handle "double 8" -> "88"
                            phone_digits = []
                            for group in groups:
                                if group.isdigit():
                                    phone_digits.append(group)
                            phone_number = ''.join(phone_digits)
                            if len(phone_number) >= 8:  # Reasonable phone number length
                                update_call_data(call_sid, contact_info=phone_number)
                                break
                    
                    # Fallback to regular patterns
                    if not call_data[call_sid].get('contact_info'):
                        phone_patterns = [
                            r'\b(\d{4}\s?\d{3}\s?\d{3})\b',
                            r'\b(\d{10})\b',
                            r'\b(\+61\s?\d{3}\s?\d{3}\s?\d{3})\b',
                        ]
                        for pattern in phone_patterns:
                            phone_match = re.search(pattern, user_prompt)
                            if phone_match:
                                update_call_data(call_sid, contact_info=phone_match.group())
                                break

        # Detect "don't know" answers and "can't tell" scenarios
        dont_know_phrases = ["don't know", "dont know", "not sure", "no idea", "can't remember", "unsure", "don't know both", "dont know both", "can't tell", "cant tell"]
        if any(phrase in user_lower for phrase in dont_know_phrases):
            # Handle "don't know both" for product and row
            if "both" in user_lower and call_data[call_sid]['issue_type'] == 'Product stuck':
                if not call_data[call_sid]['row_number']:
                    update_call_data(call_sid, row_number="Customer didn't know")
                if not call_data[call_sid]['product_name']:
                    update_call_data(call_sid, product_name="Customer didn't know")
            elif any(word in response_lower for word in ['row']) and not call_data[call_sid]['row_number']:
                update_call_data(call_sid, row_number="Customer didn't know")
            elif any(word in response_lower for word in ['amount', 'charged']) and not call_data[call_sid]['amount']:
                update_call_data(call_sid, amount="Customer didn't know")
            elif any(word in response_lower for word in ['time', 'when']) and not call_data[call_sid]['transaction_time']:
                update_call_data(call_sid, transaction_time="Customer didn't know")
            elif any(word in response_lower for word in ['digits', 'card']) and not call_data[call_sid]['last_4_digits']:
                update_call_data(call_sid, last_4_digits="Customer didn't know")
            elif any(word in response_lower for word in ['product', 'item']) and not call_data[call_sid]['product_name']:
                update_call_data(call_sid, product_name="Customer didn't know")
        
        # Special case: card stuck so can't access digits
        if call_data[call_sid].get('card_stuck') and any(phrase in user_lower for phrase in ["card stuck", "card is stuck", "stuck in the machine", "can't tell"]):
            if "digits" in user_lower or "last" in user_lower:
                update_call_data(call_sid, last_4_digits="Customer doesn't know")

        # Detect row numbers
        if not is_correction and (not call_data[call_sid]['row_number'] or call_data[call_sid]['row_number'] == "Customer didn't know"):
            # Check if we're responding to a row number question
            last_question = call_data[call_sid].get('last_question', '').lower()
            is_row_question = 'row' in last_question
            
            row_patterns = [r'\b([A-Z]\d+)\b', r'\b(row\s*[A-Z]?\d+)\b', r'\b([A-Z]{1,2}\d+)\b']
            
            # If responding to row question and it's just a number, treat as row
            # If responding to row question and it's just a number, treat as row
            if is_row_question and re.match(r'^\s*\d+\s*$', user_prompt.strip()):
                update_call_data(call_sid, row_number=user_prompt.strip())
            else:
                for pattern in row_patterns:
                    row_match = re.search(pattern, user_prompt, re.IGNORECASE)
                    if row_match:
                        update_call_data(call_sid, row_number=row_match.group(1).upper())
                        break

        # Detect last 4 digits
        if not is_correction and not call_data[call_sid].get('last_4_digits'):
            spaced_digits = re.search(r'(\d\s+){3}\d', user_prompt)
            # Handle spoken individual digits like "1, 2, 3, 4" or "one two three four"
            spoken_digits = re.search(r'(?:the\s+last\s+(?:four\s+)?digits?\s+are?\s+)?(\d)[,\s]*(\d)[,\s]*(\d)[,\s]*(\d)', user_prompt)
            
            if spaced_digits:
                digits = ''.join(spaced_digits.group().split())
                update_call_data(call_sid, last_4_digits=digits)
            elif spoken_digits:
                digits = ''.join([spoken_digits.group(i) for i in range(1, 5)])
                update_call_data(call_sid, last_4_digits=digits)
            else:
                digits_patterns = [r'(\d{4})', r'last.*?(\d{4})', r'digits.*?(\d{4})', r'card.*?(\d{4})']
                if any(word in user_lower for word in ['last', 'digits', 'card']):
                    for pattern in digits_patterns:
                        digits_match = re.search(pattern, user_prompt)
                        if digits_match:
                            potential = digits_match.group(1)
                            if potential not in [call_data[call_sid]['amount'], call_data[call_sid]['transaction_time']]:
                                update_call_data(call_sid, last_4_digits=potential)
                                break

        # Detect photo mentions
        if any(word in response_lower for word in ['photo', 'picture', 'video']):
            update_call_data(call_sid, photo_mentioned=True)

        # Update notes
        notes = []
        if call_data[call_sid]['last_4_digits']:
            notes.append(f"Last 4 digits: {call_data[call_sid]['last_4_digits']}")
        if call_data[call_sid]['photo_mentioned']:
            notes.append("Photo/video mentioned")
        if 'wallet' in response_lower or 'device account' in response_lower:
            notes.append("Wallet instructions given")
        if call_data[call_sid].get('card_stuck'):
            notes.append("Card stuck in machine")
        if is_correction:
            notes.append(f"Customer made correction: {corrected_value}")

        if notes:
            update_call_data(call_sid, notes="; ".join(notes))

        # Debug print
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
                        update_google_sheet(call_sid)
                        print(f"Successfully saved call data and updated Google Sheet for {call_sid}")
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
                update_google_sheet(call_sid)
                print(f"Successfully saved call data and updated Google Sheet for {call_sid}")
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

