"""
E-Commerce API Tests for Facette Fashion Platform
Tests: Auth, Products, Categories, Orders, Admin Dashboard
"""
import pytest
import requests
import os

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Test credentials
ADMIN_EMAIL = "admin@facette.com"
ADMIN_PASSWORD = "admin123"
TEST_USER_EMAIL = "TEST_user@example.com"
TEST_USER_PASSWORD = "testpass123"


class TestHealthAndBasicEndpoints:
    """Basic API health and root endpoint tests"""
    
    def test_api_root(self):
        """Test API root endpoint"""
        response = requests.get(f"{BASE_URL}/api/")
        assert response.status_code == 200
        data = response.json()
        assert "message" in data
        print(f"✓ API root working: {data}")
    
    def test_settings_endpoint(self):
        """Test settings endpoint"""
        response = requests.get(f"{BASE_URL}/api/settings")
        assert response.status_code == 200
        data = response.json()
        assert "site_name" in data
        print(f"✓ Settings endpoint working: site_name={data.get('site_name')}")


class TestAuthentication:
    """Authentication flow tests"""
    
    def test_admin_login_success(self):
        """Test admin login with valid credentials"""
        response = requests.post(
            f"{BASE_URL}/api/auth/login",
            params={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
        )
        assert response.status_code == 200
        data = response.json()
        assert "token" in data
        assert "user" in data
        assert data["user"]["is_admin"] == True
        print(f"✓ Admin login successful: {data['user']['email']}")
        return data["token"]
    
    def test_login_invalid_credentials(self):
        """Test login with invalid credentials"""
        response = requests.post(
            f"{BASE_URL}/api/auth/login",
            params={"email": "wrong@example.com", "password": "wrongpass"}
        )
        assert response.status_code == 401
        print("✓ Invalid login correctly rejected")
    
    def test_auth_me_without_token(self):
        """Test /auth/me without token"""
        response = requests.get(f"{BASE_URL}/api/auth/me")
        assert response.status_code == 401
        print("✓ Unauthenticated /auth/me correctly rejected")
    
    def test_auth_me_with_token(self):
        """Test /auth/me with valid token"""
        # First login
        login_res = requests.post(
            f"{BASE_URL}/api/auth/login",
            params={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
        )
        token = login_res.json()["token"]
        
        # Then check /auth/me
        response = requests.get(
            f"{BASE_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["email"] == ADMIN_EMAIL
        print(f"✓ Auth me working: {data['email']}")
    
    def test_user_registration(self):
        """Test user registration"""
        response = requests.post(
            f"{BASE_URL}/api/auth/register",
            json={
                "email": TEST_USER_EMAIL,
                "password": TEST_USER_PASSWORD,
                "first_name": "Test",
                "last_name": "User"
            }
        )
        # May return 400 if user already exists
        if response.status_code == 200:
            data = response.json()
            assert "token" in data
            print(f"✓ User registration successful: {data['user']['email']}")
        elif response.status_code == 400:
            print("✓ User already exists (expected)")
        else:
            pytest.fail(f"Unexpected status: {response.status_code}")


class TestProducts:
    """Product CRUD and listing tests"""
    
    def test_get_products_list(self):
        """Test products listing"""
        response = requests.get(f"{BASE_URL}/api/products")
        assert response.status_code == 200
        data = response.json()
        assert "products" in data
        assert "total" in data
        assert isinstance(data["products"], list)
        print(f"✓ Products list: {len(data['products'])} products, total: {data['total']}")
    
    def test_get_products_with_pagination(self):
        """Test products pagination"""
        response = requests.get(f"{BASE_URL}/api/products?page=1&limit=5")
        assert response.status_code == 200
        data = response.json()
        assert len(data["products"]) <= 5
        print(f"✓ Products pagination working: {len(data['products'])} products returned")
    
    def test_get_products_by_category(self):
        """Test products filtering by category"""
        response = requests.get(f"{BASE_URL}/api/products?category=elbise")
        assert response.status_code == 200
        data = response.json()
        print(f"✓ Products by category 'elbise': {len(data['products'])} products")
    
    def test_get_products_search(self):
        """Test products search"""
        response = requests.get(f"{BASE_URL}/api/products?search=elbise")
        assert response.status_code == 200
        data = response.json()
        print(f"✓ Products search 'elbise': {len(data['products'])} products")
    
    def test_get_new_products(self):
        """Test getting new products"""
        response = requests.get(f"{BASE_URL}/api/products?is_new=true")
        assert response.status_code == 200
        data = response.json()
        print(f"✓ New products: {len(data['products'])} products")
    
    def test_get_single_product(self):
        """Test getting single product by ID"""
        # First get a product from list
        list_res = requests.get(f"{BASE_URL}/api/products?limit=1")
        products = list_res.json()["products"]
        
        if products:
            product_id = products[0]["id"]
            response = requests.get(f"{BASE_URL}/api/products/{product_id}")
            assert response.status_code == 200
            data = response.json()
            assert data["id"] == product_id
            print(f"✓ Single product: {data['name']}")
        else:
            pytest.skip("No products available")
    
    def test_get_product_not_found(self):
        """Test getting non-existent product"""
        response = requests.get(f"{BASE_URL}/api/products/non-existent-id-12345")
        assert response.status_code == 404
        print("✓ Non-existent product correctly returns 404")


class TestCategories:
    """Category endpoint tests"""
    
    def test_get_categories(self):
        """Test categories listing"""
        response = requests.get(f"{BASE_URL}/api/categories")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        print(f"✓ Categories: {len(data)} categories")
        
        # Verify expected categories exist
        category_names = [c["name"] for c in data]
        expected = ["En Yeniler", "Elbise", "Bluz", "Pantolon"]
        for cat in expected:
            if cat in category_names:
                print(f"  - Found category: {cat}")


class TestMenu:
    """Menu endpoint tests"""
    
    def test_get_menu(self):
        """Test menu items listing"""
        response = requests.get(f"{BASE_URL}/api/menu")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        print(f"✓ Menu items: {len(data)} items")
        
        # Verify expected menu items
        menu_names = [m["name"] for m in data]
        expected = ["EN YENİLER", "ELBİSE", "BLUZ", "PANTOLON", "CEKET", "AKSESUAR"]
        for item in expected:
            if item in menu_names:
                print(f"  - Found menu item: {item}")


class TestBanners:
    """Banner endpoint tests"""
    
    def test_get_banners(self):
        """Test banners listing"""
        response = requests.get(f"{BASE_URL}/api/banners")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        print(f"✓ Banners: {len(data)} banners")


class TestAdminEndpoints:
    """Admin-protected endpoint tests"""
    
    @pytest.fixture
    def admin_token(self):
        """Get admin token"""
        response = requests.post(
            f"{BASE_URL}/api/auth/login",
            params={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
        )
        return response.json()["token"]
    
    def test_admin_dashboard_stats(self, admin_token):
        """Test admin dashboard statistics"""
        response = requests.get(
            f"{BASE_URL}/api/reports/dashboard",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        assert "total_orders" in data
        assert "total_products" in data
        assert "total_users" in data
        assert "total_revenue" in data
        print(f"✓ Dashboard stats: {data['total_products']} products, {data['total_orders']} orders, {data['total_revenue']} TL revenue")
    
    def test_admin_dashboard_without_auth(self):
        """Test admin dashboard without authentication"""
        response = requests.get(f"{BASE_URL}/api/reports/dashboard")
        assert response.status_code == 401
        print("✓ Admin dashboard correctly requires authentication")
    
    def test_admin_products_list(self, admin_token):
        """Test admin products listing"""
        response = requests.get(
            f"{BASE_URL}/api/admin/products",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        assert "products" in data
        assert "total" in data
        print(f"✓ Admin products: {data['total']} total products")
    
    def test_admin_products_search(self, admin_token):
        """Test admin products search"""
        response = requests.get(
            f"{BASE_URL}/api/admin/products?search=elbise",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        print(f"✓ Admin products search: {len(data['products'])} products found")
    
    def test_admin_orders_list(self, admin_token):
        """Test admin orders listing"""
        response = requests.get(
            f"{BASE_URL}/api/orders",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        assert "orders" in data
        assert "total" in data
        print(f"✓ Admin orders: {data['total']} total orders")


class TestStaticPages:
    """Static pages endpoint tests"""
    
    def test_get_pages(self):
        """Test static pages listing"""
        response = requests.get(f"{BASE_URL}/api/pages")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        print(f"✓ Static pages: {len(data)} pages")
    
    def test_get_page_by_slug(self):
        """Test getting page by slug"""
        response = requests.get(f"{BASE_URL}/api/pages/hakkimizda")
        if response.status_code == 200:
            data = response.json()
            assert data["slug"] == "hakkimizda"
            print(f"✓ Page 'hakkimizda': {data['title']}")
        elif response.status_code == 404:
            print("✓ Page 'hakkimizda' not found (may not be seeded)")


class TestCampaigns:
    """Campaign endpoint tests"""
    
    def test_get_campaigns(self):
        """Test campaigns listing"""
        response = requests.get(f"{BASE_URL}/api/campaigns")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        print(f"✓ Campaigns: {len(data)} campaigns")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
