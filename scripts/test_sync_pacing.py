import os
import sys
import time
from pathlib import Path

# Add project root to sys.path
sys.path.append(str(Path(__file__).parent.parent))

from app.providers import get_provider

def test_pacing():
    print("Starting Gemini Rate Limit Stress Test...")
    provider = get_provider()
    
    # Simulate a few rapid-fire calls similar to a sync run
    for i in range(5):
        print(f"\n[Test] Call {i+1}/5...")
        try:
            start = time.time()
            # Minimal prompt to test the connection/rate limit
            res = provider.generate("Hi, just checking if you are up. Reply with 'OK'.")
            duration = time.time() - start
            print(f"[Test] Response: {res.strip()} (took {duration:.1f}s)")
        except Exception as e:
            print(f"[Test] FAILED: {e}")
            sys.exit(1)
        
        if i < 4:
            print("[Test] Pacing delay (5s)...")
            time.sleep(5)

    print("\n✅ Verification Passed: Gemini handled sequential calls with pacing.")

if __name__ == "__main__":
    if not os.environ.get("GEMINI_API_KEY"):
        print("Error: GEMINI_API_KEY env var must be set for this test.")
        sys.exit(1)
    test_pacing()
