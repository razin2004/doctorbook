import os
import jinja2

def main():
    try:
        template_dir = 'templates'
        loader = jinja2.FileSystemLoader(template_dir)
        env = jinja2.Environment(loader=loader)
        
        # Test compiling booking.html
        print("Compiling booking.html...")
        template = env.get_template('booking.html')
        print("Success! booking.html compiled without syntax errors.")
        
        # Mock render with typical context variables
        print("Testing dry run render of booking.html...")
        dummy_context = {
            'is_admin_view': True,
            'admin_email': 'admin@primecare.com',
            'session': {'user_id': 1, 'user_role': 'admin'},
            'get_flashed_messages': lambda **kwargs: []
        }
        rendered = template.render(dummy_context)
        print(f"Jinja template rendered successfully. Size: {len(rendered)} characters.")
        
    except Exception as e:
        print("Error during template parsing/compiling:")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    main()
