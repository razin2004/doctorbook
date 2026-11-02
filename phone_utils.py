import re

def normalize_phone_number(phone_str, default_country_code="+91"):
    """
    Normalizes a mobile phone number into E.164 international format.
    Default country code is +91 (India) if no country code is specified.
    
    Examples:
        '9876543210'        -> '+919876543210'
        '+91 98765 43210'   -> '+919876543210'
        '09876543210'       -> '+919876543210'
        '919876543210'      -> '+919876543210'
        '+1 (415) 555-2671' -> '+14155552671'
    
    Returns:
        Cleaned E.164 string like '+919876543210' on success, or None on failure.
    """
    if not phone_str:
        return None
    
    # Strip whitespace, dots, hyphens, parentheses
    cleaned = re.sub(r'[\s\.\-\(\)]', '', str(phone_str).strip())
    
    if not cleaned:
        return None
        
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    elif cleaned.startswith("+"):
        pass
    elif cleaned.startswith("0") and len(cleaned) == 11 and cleaned[1:].isdigit():
        cleaned = default_country_code + cleaned[1:]
    elif len(cleaned) == 10 and cleaned.isdigit():
        cleaned = default_country_code + cleaned
    elif len(cleaned) == 12 and cleaned.startswith("91") and cleaned.isdigit():
        cleaned = "+" + cleaned
    else:
        if cleaned.isdigit():
            cleaned = "+" + cleaned

    # E.164 validation: + followed by 8 to 15 digits, starting with non-zero
    if not re.match(r'^\+[1-9]\d{7,14}$', cleaned):
        return None

    return cleaned

def validate_phone_number(phone_str, default_country_code="+91"):
    """
    Validates and normalizes phone number.
    Returns (normalized_phone, error_message)
    """
    norm = normalize_phone_number(phone_str, default_country_code)
    if norm:
        return norm, None
    return None, "Invalid mobile number format. Please provide a valid mobile number with country code."

def format_phone_display(phone_str):
    """
    Formats an E.164 phone number into a user-friendly display string.
    Example: '+919876543210' -> '+91 98765 43210'
    """
    if not phone_str:
        return ""
    phone = str(phone_str).strip()
    if phone.startswith("+91") and len(phone) == 13:
        return f"+91 {phone[3:8]} {phone[8:]}"
    elif phone.startswith("+1") and len(phone) == 12:
        return f"+1 ({phone[2:5]}) {phone[5:8]}-{phone[8:]}"
    return phone

if __name__ == '__main__':
    # Quick self-test
    test_cases = [
        ("9876543210", "+919876543210"),
        ("+91 98765 43210", "+919876543210"),
        ("09876543210", "+919876543210"),
        ("919876543210", "+919876543210"),
        ("+1 (415) 555-2671", "+14155552671"),
        ("invalid", None),
        ("", None),
        ("123", None)
    ]
    all_passed = True
    for raw, expected in test_cases:
        res = normalize_phone_number(raw)
        if res != expected:
            print(f"FAIL: {raw} -> {res} (expected {expected})")
            all_passed = False
    if all_passed:
        print("phone_utils: All self-tests passed!")
