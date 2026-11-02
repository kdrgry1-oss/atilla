"""
Test suite for Facette E-Commerce API after modular refactoring
Tests: Auth, Products, Categories, CMS, Orders, Admin, Customer, Integrations
"""
import pytest
import requests
import os

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Test credentials
ADMIN_EMAIL = "admin@facette.com"
ADMIN_PASSWORD = "admin123"


class TestHealthAndRoot:
    """Health check and root endpoint tests"""
    
    def test_health_endpoint(self):
        """Test /api/health returns healthy status"""
        response = requests.get(f"{BASE_URL}/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        print("✓ Health endpoint working")
    
    def test_root_endpoint(self):
        """Test /api/ returns API info"""
        response = requests.get(f"{BASE_URL}/api/")
        assert response.status_code == 200
        data = response.json()
        assert "message" in data
        assert "version" in data
        print("✓ Root endpoint working")


class TestAuthEndpoints:
    """Authentication endpoint tests - Login, Register, Me"""
    
    def test_login_with_valid_credentials(self):
        """Test login with admin credentials via query params"""
        response = requests.post(
            f"{BASE_URL}/api/auth/login?email={ADMIN_EMAIL}&password={ADMIN_PASSWORD}"
        )
        assert response.status_code == 200
        data = response.json()
        assert "token" in data
        assert "user" in data
        assert data["user"]["email"] == ADMIN_EMAIL
        assert data["user"]["is_admin"] == True
        print(f"✓ Login successful for {ADMIN_EMAIL}")
        return data["token"]
    
    def test_login_with_invalid_credentials(self):
        """Test login with wrong password"""
        response = requests.post(
            f"{BASE_URL}/api/auth/login?email={ADMIN_EMAIL}&password=wrongpassword"
        )
        assert response.status_code == 401
        print("✓ Invalid credentials rejected correctly")
    
    def test_me_endpoint_with_token(self):
        """Test /auth/me returns user info with valid token"""
        # First login to get token
        login_response = requests.post(
            f"{BASE_URL}/api/auth/login?email={ADMIN_EMAIL}&password={ADMIN_PASSWORD}"
        )
        token = login_response.json()["token"]
        
        # Test /me endpoint
        response = requests.get(
            f"{BASE_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["email"] == ADMIN_EMAIL
        assert data["is_admin"] == True
        print("✓ /auth/me endpoint working with token")
    
    def test_me_endpoint_without_token(self):
        """Test /auth/me returns 401 without token"""
        response = requests.get(f"{BASE_URL}/api/auth/me")
        assert response.status_code == 401
        print("✓ /auth/me correctly requires authentication")


class TestProductsEndpoints:
    """Products CRUD and listing tests"""
    
    def test_get_products_list(self):
        """Test products listing with pagination"""
        response = requests.get(f"{BASE_URL}/api/products?page=1&limit=5")
        assert response.status_code == 200
        data = response.json()
        assert "products" in data
        assert "total" in data
        assert "page" in data
        assert "pages" in data
        assert isinstance(data["products"], list)
        print(f"✓ Products list returned {len(data['products'])} items, total: {data['total']}")
    
    def test_get_products_with_filters(self):
        """Test products filtering by category"""
        response = requests.get(f"{BASE_URL}/api/products?category=jean&limit=5")
        assert response.status_code == 200
        data = response.json()
        assert "products" in data
        print(f"✓ Products filter by category working, found {len(data['products'])} items")
    
    def test_get_single_product(self):
        """Test getting single product by ID"""
        # First get a product ID from list
        list_response = requests.get(f"{BASE_URL}/api/products?limit=1")
        products = list_response.json()["products"]
        if products:
            product_id = products[0]["id"]
            response = requests.get(f"{BASE_URL}/api/products/{product_id}")
            assert response.status_code == 200
            data = response.json()
            assert data["id"] == product_id
            print(f"✓ Single product fetch working for ID: {product_id}")
        else:
            pytest.skip("No products available for testing")
    
    def test_create_product_requires_admin(self):
        """Test product creation requires admin auth"""
        response = requests.post(
            f"{BASE_URL}/api/products",
            json={"name": "Test Product", "price": 100}
        )
        assert response.status_code == 401
        print("✓ Product creation correctly requires admin auth")


class TestCategoriesEndpoints:
    """Categories listing tests"""
    
    def test_get_categories(self):
        """Test categories listing"""
        response = requests.get(f"{BASE_URL}/api/categories")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        if data:
            assert "name" in data[0]
            assert "slug" in data[0]
        print(f"✓ Categories list returned {len(data)} items")


class TestCMSEndpoints:
    """CMS page-blocks tests"""
    
    def test_get_homepage_blocks(self):
        """Test getting homepage CMS blocks"""
        response = requests.get(f"{BASE_URL}/api/page-blocks?page=home")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        print(f"✓ Homepage blocks returned {len(data)} items")
    
    def test_page_blocks_structure(self):
        """Test page blocks have correct structure"""
        response = requests.get(f"{BASE_URL}/api/page-blocks?page=home")
        data = response.json()
        if data:
            block = data[0]
            assert "id" in block
            assert "type" in block
            assert "is_active" in block
            print(f"✓ Page blocks have correct structure")
        else:
            print("✓ No page blocks to verify structure (empty list)")


class TestOrdersEndpoints:
    """Orders CRUD tests (admin only)"""
    
    @pytest.fixture
    def admin_token(self):
        """Get admin token for authenticated requests"""
        response = requests.post(
            f"{BASE_URL}/api/auth/login?email={ADMIN_EMAIL}&password={ADMIN_PASSWORD}"
        )
        return response.json()["token"]
    
    def test_get_orders_requires_admin(self):
        """Test orders listing requires admin auth"""
        response = requests.get(f"{BASE_URL}/api/orders")
        assert response.status_code == 401
        print("✓ Orders listing correctly requires admin auth")
    
    def test_get_orders_with_admin(self, admin_token):
        """Test orders listing with admin token"""
        response = requests.get(
            f"{BASE_URL}/api/orders?page=1&limit=5",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        assert "orders" in data
        assert "total" in data
        print(f"✓ Orders list returned {len(data['orders'])} items, total: {data['total']}")
    
    def test_create_order(self, admin_token):
        """Test order creation"""
        order_data = {
            "items": [{"product_id": "test-123", "name": "TEST_Product", "quantity": 1, "price": 100}],
            "shipping_address": {
                "first_name": "TEST",
                "last_name": "User",
                "phone": "5551234567",
                "address": "Test Address",
                "city": "Istanbul",
                "district": "Kadikoy"
            },
            "total": 100
        }
        response = requests.post(
            f"{BASE_URL}/api/orders",
            json=order_data,
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        assert "order_id" in data
        assert "order_number" in data
        print(f"✓ Order created: {data['order_number']}")
        return data["order_id"]


class TestAdminEndpoints:
    """Admin dashboard stats tests"""
    
    @pytest.fixture
    def admin_token(self):
        """Get admin token for authenticated requests"""
        response = requests.post(
            f"{BASE_URL}/api/auth/login?email={ADMIN_EMAIL}&password={ADMIN_PASSWORD}"
        )
        return response.json()["token"]
    
    def test_dashboard_stats_requires_admin(self):
        """Test dashboard stats requires admin auth"""
        response = requests.get(f"{BASE_URL}/api/admin/dashboard-stats")
        assert response.status_code == 401
        print("✓ Dashboard stats correctly requires admin auth")
    
    def test_dashboard_stats_with_admin(self, admin_token):
        """Test dashboard stats with admin token"""
        response = requests.get(
            f"{BASE_URL}/api/admin/dashboard-stats",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        assert "total_orders" in data
        assert "total_revenue" in data
        assert "total_products" in data
        assert "total_customers" in data
        assert "recent_orders" in data
        print(f"✓ Dashboard stats: {data['total_orders']} orders, {data['total_products']} products")


class TestCustomerEndpoints:
    """Customer account endpoints tests"""
    
    @pytest.fixture
    def admin_token(self):
        """Get admin token for authenticated requests"""
        response = requests.post(
            f"{BASE_URL}/api/auth/login?email={ADMIN_EMAIL}&password={ADMIN_PASSWORD}"
        )
        return response.json()["token"]
    
    def test_my_orders_requires_auth(self):
        """Test my-orders requires authentication"""
        response = requests.get(f"{BASE_URL}/api/my-orders")
        assert response.status_code == 401
        print("✓ my-orders correctly requires authentication")
    
    def test_my_orders_with_auth(self, admin_token):
        """Test my-orders with authentication"""
        response = requests.get(
            f"{BASE_URL}/api/my-orders",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        assert "orders" in data
        assert "total" in data
        print(f"✓ my-orders returned {len(data['orders'])} orders")
    
    def test_my_addresses_requires_auth(self):
        """Test my-addresses requires authentication"""
        response = requests.get(f"{BASE_URL}/api/my-addresses")
        assert response.status_code == 401
        print("✓ my-addresses correctly requires authentication")
    
    def test_my_addresses_with_auth(self, admin_token):
        """Test my-addresses with authentication"""
        response = requests.get(
            f"{BASE_URL}/api/my-addresses",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        assert "addresses" in data
        print(f"✓ my-addresses returned {len(data['addresses'])} addresses")
    
    def test_create_address(self, admin_token):
        """Test address creation"""
        address_data = {
            "title": "TEST_Home",
            "first_name": "TEST",
            "last_name": "User",
            "phone": "5551234567",
            "address": "Test Street 123",
            "city": "Istanbul",
            "district": "Kadikoy",
            "is_default": False
        }
        response = requests.post(
            f"{BASE_URL}/api/addresses",
            json=address_data,
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] == True
        assert "address_id" in data
        print(f"✓ Address created: {data['address_id']}")


class TestIntegrationStatusEndpoints:
    """Integration status endpoints tests (Iyzico, Trendyol, GIB)"""
    
    def test_payment_status(self):
        """Test Iyzico payment status endpoint"""
        response = requests.get(f"{BASE_URL}/api/payment/status")
        assert response.status_code == 200
        data = response.json()
        assert "mode" in data
        assert "configured" in data
        print(f"✓ Payment status: mode={data['mode']}, configured={data['configured']}")
    
    def test_trendyol_status(self):
        """Test Trendyol integration status endpoint"""
        response = requests.get(f"{BASE_URL}/api/trendyol/status")
        assert response.status_code == 200
        data = response.json()
        assert "mode" in data
        assert "configured" in data
        print(f"✓ Trendyol status: mode={data['mode']}, configured={data['configured']}")
    
    def test_gib_status(self):
        """Test GIB E-Fatura status endpoint"""
        response = requests.get(f"{BASE_URL}/api/gib/status")
        assert response.status_code == 200
        data = response.json()
        assert "mode" in data
        assert "configured" in data
        print(f"✓ GIB status: mode={data['mode']}, configured={data['configured']}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
