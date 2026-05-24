from app import app, db, get_all_settings, AppSettings, get_all_doctors

with app.app_context():
    # Fetch all settings
    settings = get_all_settings()
    print("Settings keys:", list(settings.keys()))
    print("Departments count:", settings.get("departments_count"))
    
    # Verify departments count is an integer and matches unique specs or fallback
    assert isinstance(settings["departments_count"], int), "departments_count must be an integer"
    
    # Try fetching doctors directly to compare counts
    docs = get_all_doctors()
    unique_specs = len(set(d.get("Specialization", "").strip() for d in docs if d.get("Specialization", "").strip()))
    expected = unique_specs if unique_specs else 10
    
    print(f"Calculated unique specializations: {unique_specs}")
    print(f"Expected count: {expected}, Got: {settings['departments_count']}")
    assert settings['departments_count'] == expected, "Departments count mismatch!"
    
    print("All tests passed successfully!")
