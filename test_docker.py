import httpx
import time

def test_api():
    print("Testing Scoutly Docker API...")
    try:
        response = httpx.get("http://localhost:8000/api/health")
        print(f"Health Endpoint: {response.status_code}")
        print(response.json())
        assert response.status_code == 200
        print("✅ Backend API is healthy and reachable!")
    except Exception as e:
        print(f"❌ Failed to reach Backend API: {e}")

if __name__ == "__main__":
    test_api()
