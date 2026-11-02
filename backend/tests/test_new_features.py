"""
Tests for New Features: Variants, Similar/Combo Products, Cargo, Ship Order
Facette E-Commerce Platform - Iteration 3
"""
import pytest
import requests
import os

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Test credentials
ADMIN_EMAIL = "admin@facette.com"
ADMIN_PASSWORD = "admin123"


@pytest.fixture(scope="module")
def admin_token():
    """Get admin token for authenticated requests"""
    response = requests.post(
        f"{BASE_URL}/api/auth/login",
        params={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 200, f"Admin login failed: {response.text}"
    return response.json()["token"]


@pytest.fixture(scope="module")
def sample_product():
    """Get a sample product for testing"""
    response = requests.get(f"{BASE_URL}/api/products?limit=1")
    assert response.status_code == 200
    products = response.json().get("products", [])
    assert len(products) > 0, "No products available for testing"
    return products[0]


@pytest.fixture(scope="module")
def sample_order(admin_token):
    """Get a sample order for testing"""
    response = requests.get(
        f"{BASE_URL}/api/orders",
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert response.status_code == 200
    orders = response.json().get("orders", [])
    if len(orders) > 0:
        return orders[0]
    return None


class TestCargoCompanies:
    """Tests for /api/cargo/companies endpoint"""
    
    def test_get_cargo_companies(self):
        """Test getting list of cargo companies"""
        response = requests.get(f"{BASE_URL}/api/cargo/companies")
        assert response.status_code == 200
        
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 5, "Expected at least 5 cargo companies"
        
        # Verify expected companies
        company_codes = [c["code"] for c in data]
        expected_codes = ["MNG", "DHL", "YURTICI", "ARAS", "PTT"]
        for code in expected_codes:
            assert code in company_codes, f"Missing cargo company: {code}"
        
        # Verify structure
        for company in data:
            assert "code" in company
            assert "name" in company
            assert "tracking_url" in company
        
        print(f"✓ Cargo companies: {len(data)} companies returned")
        print(f"  Companies: {', '.join(company_codes)}")


class TestSimilarProducts:
    """Tests for /api/products/{id}/similar endpoint"""
    
    def test_get_similar_products_by_id(self, sample_product):
        """Test getting similar products by product ID"""
        product_id = sample_product["id"]
        response = requests.get(f"{BASE_URL}/api/products/{product_id}/similar?limit=4")
        assert response.status_code == 200
        
        data = response.json()
        assert isinstance(data, list)
        
        # Similar products should not include the original product
        similar_ids = [p["id"] for p in data]
        assert product_id not in similar_ids, "Similar products should not include original product"
        
        print(f"✓ Similar products for '{sample_product['name'][:30]}...': {len(data)} products")
    
    def test_get_similar_products_by_slug(self, sample_product):
        """Test getting similar products by product slug"""
        product_slug = sample_product["slug"]
        response = requests.get(f"{BASE_URL}/api/products/{product_slug}/similar?limit=4")
        assert response.status_code == 200
        
        data = response.json()
        assert isinstance(data, list)
        print(f"✓ Similar products by slug: {len(data)} products")
    
    def test_similar_products_not_found(self):
        """Test similar products for non-existent product"""
        response = requests.get(f"{BASE_URL}/api/products/non-existent-product-id/similar")
        assert response.status_code == 404
        print("✓ Non-existent product correctly returns 404")


class TestComboProducts:
    """Tests for /api/products/{id}/combo endpoint"""
    
    def test_get_combo_products_by_id(self, sample_product):
        """Test getting combo products by product ID"""
        product_id = sample_product["id"]
        response = requests.get(f"{BASE_URL}/api/products/{product_id}/combo?limit=4")
        assert response.status_code == 200
        
        data = response.json()
        assert isinstance(data, list)
        
        # Combo products should not include the original product
        combo_ids = [p["id"] for p in data]
        assert product_id not in combo_ids, "Combo products should not include original product"
        
        print(f"✓ Combo products for '{sample_product['name'][:30]}...': {len(data)} products")
    
    def test_get_combo_products_by_slug(self, sample_product):
        """Test getting combo products by product slug"""
        product_slug = sample_product["slug"]
        response = requests.get(f"{BASE_URL}/api/products/{product_slug}/combo?limit=4")
        assert response.status_code == 200
        
        data = response.json()
        assert isinstance(data, list)
        print(f"✓ Combo products by slug: {len(data)} products")
    
    def test_combo_products_not_found(self):
        """Test combo products for non-existent product"""
        response = requests.get(f"{BASE_URL}/api/products/non-existent-product-id/combo")
        assert response.status_code == 404
        print("✓ Non-existent product correctly returns 404")


class TestShipOrder:
    """Tests for /api/orders/{id}/ship endpoint"""
    
    def test_ship_order_success(self, admin_token, sample_order):
        """Test shipping an order with valid data"""
        if not sample_order:
            pytest.skip("No orders available for testing")
        
        order_id = sample_order["id"]
        response = requests.post(
            f"{BASE_URL}/api/orders/{order_id}/ship",
            params={"cargo_company": "MNG", "tracking_number": "MNG123456789012"},
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        
        data = response.json()
        assert data["success"] == True
        assert "tracking_url" in data
        assert "MNG" in data["tracking_url"]
        assert "message" in data
        
        print(f"✓ Ship order success: {data['message']}")
        print(f"  Tracking URL: {data['tracking_url']}")
    
    def test_ship_order_different_companies(self, admin_token, sample_order):
        """Test shipping with different cargo companies"""
        if not sample_order:
            pytest.skip("No orders available for testing")
        
        order_id = sample_order["id"]
        companies = ["DHL", "YURTICI", "ARAS", "PTT"]
        
        for company in companies:
            response = requests.post(
                f"{BASE_URL}/api/orders/{order_id}/ship",
                params={"cargo_company": company, "tracking_number": f"{company}123456789012"},
                headers={"Authorization": f"Bearer {admin_token}"}
            )
            assert response.status_code == 200
            data = response.json()
            assert data["success"] == True
            print(f"✓ Ship with {company}: Success")
    
    def test_ship_order_invalid_company(self, admin_token, sample_order):
        """Test shipping with invalid cargo company"""
        if not sample_order:
            pytest.skip("No orders available for testing")
        
        order_id = sample_order["id"]
        response = requests.post(
            f"{BASE_URL}/api/orders/{order_id}/ship",
            params={"cargo_company": "INVALID_COMPANY", "tracking_number": "TEST123"},
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 400
        print("✓ Invalid cargo company correctly rejected")
    
    def test_ship_order_not_found(self, admin_token):
        """Test shipping non-existent order"""
        response = requests.post(
            f"{BASE_URL}/api/orders/non-existent-order-id/ship",
            params={"cargo_company": "MNG", "tracking_number": "MNG123"},
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 404
        print("✓ Non-existent order correctly returns 404")
    
    def test_ship_order_without_auth(self, sample_order):
        """Test shipping without authentication"""
        if not sample_order:
            pytest.skip("No orders available for testing")
        
        order_id = sample_order["id"]
        response = requests.post(
            f"{BASE_URL}/api/orders/{order_id}/ship",
            params={"cargo_company": "MNG", "tracking_number": "MNG123"}
        )
        assert response.status_code == 401
        print("✓ Ship order correctly requires authentication")


class TestProductVariants:
    """Tests for product variant management endpoints"""
    
    def test_add_variant_to_product(self, admin_token, sample_product):
        """Test adding a variant to a product"""
        product_id = sample_product["id"]
        variant_data = {
            "size": "M",
            "color": "Siyah",
            "barcode": "TEST_BARCODE_001",
            "stock": 10,
            "price_adjustment": 0
        }
        
        response = requests.post(
            f"{BASE_URL}/api/products/{product_id}/variants",
            json=variant_data,
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        
        data = response.json()
        assert data["success"] == True
        assert "variant" in data
        assert data["variant"]["size"] == "M"
        assert data["variant"]["color"] == "Siyah"
        
        print(f"✓ Variant added: Size={data['variant']['size']}, Color={data['variant']['color']}")
        return data["variant"]["id"]
    
    def test_add_variant_without_auth(self, sample_product):
        """Test adding variant without authentication"""
        product_id = sample_product["id"]
        variant_data = {"size": "L", "stock": 5}
        
        response = requests.post(
            f"{BASE_URL}/api/products/{product_id}/variants",
            json=variant_data
        )
        assert response.status_code == 401
        print("✓ Add variant correctly requires authentication")
    
    def test_add_variant_to_nonexistent_product(self, admin_token):
        """Test adding variant to non-existent product"""
        variant_data = {"size": "M", "stock": 5}
        
        response = requests.post(
            f"{BASE_URL}/api/products/non-existent-product-id/variants",
            json=variant_data,
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 404
        print("✓ Non-existent product correctly returns 404")


class TestOrderTracking:
    """Tests for order tracking endpoint"""
    
    def test_track_order_by_id(self, sample_order):
        """Test tracking order by ID"""
        if not sample_order:
            pytest.skip("No orders available for testing")
        
        order_id = sample_order["id"]
        response = requests.get(f"{BASE_URL}/api/orders/{order_id}/track")
        assert response.status_code == 200
        
        data = response.json()
        assert "status" in data
        print(f"✓ Order tracking: status={data['status']}")
        
        if data.get("cargo_company"):
            print(f"  Cargo: {data['cargo_company']}, Tracking: {data.get('tracking_number')}")
    
    def test_track_order_by_number(self, sample_order):
        """Test tracking order by order number"""
        if not sample_order:
            pytest.skip("No orders available for testing")
        
        order_number = sample_order["order_number"]
        response = requests.get(f"{BASE_URL}/api/orders/{order_number}/track")
        assert response.status_code == 200
        
        data = response.json()
        assert "status" in data
        print(f"✓ Order tracking by number: status={data['status']}")
    
    def test_track_nonexistent_order(self):
        """Test tracking non-existent order"""
        response = requests.get(f"{BASE_URL}/api/orders/non-existent-order/track")
        assert response.status_code == 404
        print("✓ Non-existent order correctly returns 404")


class TestSetSimilarComboProducts:
    """Tests for setting similar/combo products (admin)"""
    
    def test_set_similar_products(self, admin_token, sample_product):
        """Test setting similar products for a product"""
        product_id = sample_product["id"]
        
        # Get another product to set as similar
        response = requests.get(f"{BASE_URL}/api/products?limit=5")
        products = response.json()["products"]
        other_ids = [p["id"] for p in products if p["id"] != product_id][:2]
        
        if not other_ids:
            pytest.skip("Not enough products for testing")
        
        response = requests.put(
            f"{BASE_URL}/api/products/{product_id}/similar",
            json=other_ids,
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] == True
        print(f"✓ Set similar products: {len(other_ids)} products linked")
    
    def test_set_combo_products(self, admin_token, sample_product):
        """Test setting combo products for a product"""
        product_id = sample_product["id"]
        
        # Get another product to set as combo
        response = requests.get(f"{BASE_URL}/api/products?limit=5")
        products = response.json()["products"]
        other_ids = [p["id"] for p in products if p["id"] != product_id][:2]
        
        if not other_ids:
            pytest.skip("Not enough products for testing")
        
        response = requests.put(
            f"{BASE_URL}/api/products/{product_id}/combo",
            json=other_ids,
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] == True
        print(f"✓ Set combo products: {len(other_ids)} products linked")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
