import requests

def scrape_blinkit(item, location):
    url = "https://blinkit.com/v1/search"

    params = {
        "q": item
    }

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json"
    }

    r = requests.get(url, params=params, headers=headers)
    data = r.json()

    results = []

    for product in data.get("products", []):
        results.append({
            "name": product.get("name"),
            "quantity": product.get("pack_size"),
            "selling_price": f"₹{product.get('price')}",
            "mrp": f"₹{product.get('mrp')}",
            "discount": product.get("discount"),
            "image_url": product.get("image_url"),
            "product_url": product.get("url"),
            "source": "blinkit"
        })

    return results
print(scrape_blinkit("oil","hyderabad"))