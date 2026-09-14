"""测试两个模型连接"""
import urllib.request, json

def test(model_key):
    url = "http://localhost:5000/api/test-model"
    req = urllib.request.Request(
        url,
        data=json.dumps({"model": model_key}).encode(),
        headers={"Content-Type": "application/json"}
    )
    try:
        r = urllib.request.urlopen(req, timeout=60)
        result = json.loads(r.read())
        status = "OK" if result.get("status") == "ok" else "FAIL"
        print(f"\n{'='*40}")
        print(f"[{status}] {model_key}")
        print(f"  URL:  {result.get('base_url', '?')}")
        print(f"  Model: {result.get('model_id', '?')}")
        if result.get("status") == "ok":
            print(f"  Reply: {result.get('reply', '')[:100]}")
        else:
            print(f"  Error: {result.get('error', '')[:200]}")
    except Exception as e:
        print(f"[FAIL] {model_key}: {e}")

test("sensenova")
print()