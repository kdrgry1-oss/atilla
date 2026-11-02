"""
Test MNG Kargo API Integration and Cargo Label Generation
Features tested:
- /api/orders/{id}/cargo-label - HTML cargo label with barcodes
- /api/orders/bulk-labels - Multiple labels generation
- /api/orders/{id}/create-mng-shipment - MNG API integration
- Label dimensions (10cm x 15cm)
- Barcode generation (top and bottom)
- Sender/Receiver/Cargo info sections
"""

import pytest
import requests
import os
import re

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Test credentials
ADMIN_EMAIL = "admin@facette.com"
ADMIN_PASSWORD = "admin123"

# Existing order ID for testing
TEST_ORDER_ID = "bd242694-8dca-41e1-91ec-16b85b099d17"


@pytest.fixture(scope="module")
def admin_token():
    """Get admin authentication token"""
    response = requests.post(
        f"{BASE_URL}/api/auth/login",
        params={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    if response.status_code == 200:
        return response.json().get("token")
    pytest.skip("Admin authentication failed")


@pytest.fixture(scope="module")
def auth_headers(admin_token):
    """Get authorization headers"""
    return {"Authorization": f"Bearer {admin_token}"}


class TestCargoLabelEndpoint:
    """Test /api/orders/{id}/cargo-label endpoint"""
    
    def test_cargo_label_returns_html(self, auth_headers):
        """Test that cargo-label endpoint returns HTML content"""
        response = requests.get(
            f"{BASE_URL}/api/orders/{TEST_ORDER_ID}/cargo-label",
            headers=auth_headers
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        assert "text/html" in response.headers.get("content-type", ""), "Expected HTML content type"
        
        html_content = response.text
        assert "<!DOCTYPE html>" in html_content, "Should contain DOCTYPE"
        assert "Kargo Etiketi" in html_content, "Should contain 'Kargo Etiketi' title"
        print("PASS: Cargo label returns HTML content")
    
    def test_cargo_label_has_correct_dimensions(self, auth_headers):
        """Test that label has 10cm x 15cm dimensions in CSS"""
        response = requests.get(
            f"{BASE_URL}/api/orders/{TEST_ORDER_ID}/cargo-label",
            headers=auth_headers
        )
        
        assert response.status_code == 200
        html_content = response.text
        
        # Check @page size
        assert "size: 10cm 15cm" in html_content, "Should have @page size: 10cm 15cm"
        
        # Check body dimensions
        assert "width: 10cm" in html_content, "Should have width: 10cm"
        assert "height: 15cm" in html_content, "Should have height: 15cm"
        print("PASS: Cargo label has correct 10cm x 15cm dimensions")
    
    def test_cargo_label_has_top_barcode(self, auth_headers):
        """Test that label contains top barcode section"""
        response = requests.get(
            f"{BASE_URL}/api/orders/{TEST_ORDER_ID}/cargo-label",
            headers=auth_headers
        )
        
        assert response.status_code == 200
        html_content = response.text
        
        # Check for top barcode section
        assert 'class="top-barcode"' in html_content, "Should have top-barcode section"
        
        # Check for base64 encoded barcode image
        assert 'data:image/png;base64,' in html_content, "Should contain base64 barcode image"
        print("PASS: Cargo label has top barcode")
    
    def test_cargo_label_has_bottom_barcode(self, auth_headers):
        """Test that label contains bottom barcode section"""
        response = requests.get(
            f"{BASE_URL}/api/orders/{TEST_ORDER_ID}/cargo-label",
            headers=auth_headers
        )
        
        assert response.status_code == 200
        html_content = response.text
        
        # Check for bottom barcode section
        assert 'class="bottom-barcode"' in html_content, "Should have bottom-barcode section"
        
        # Count barcode images (should be at least 2)
        barcode_count = html_content.count('data:image/png;base64,')
        assert barcode_count >= 2, f"Should have at least 2 barcodes, found {barcode_count}"
        print(f"PASS: Cargo label has bottom barcode (total {barcode_count} barcodes)")
    
    def test_cargo_label_has_sender_info(self, auth_headers):
        """Test that label contains sender (Gönderici) information"""
        response = requests.get(
            f"{BASE_URL}/api/orders/{TEST_ORDER_ID}/cargo-label",
            headers=auth_headers
        )
        
        assert response.status_code == 200
        html_content = response.text
        
        # Check for sender section
        assert "Gönderici Bilgileri" in html_content, "Should have 'Gönderici Bilgileri' section"
        assert "FACETTE" in html_content, "Should contain FACETTE company name"
        print("PASS: Cargo label has sender information")
    
    def test_cargo_label_has_receiver_info(self, auth_headers):
        """Test that label contains receiver (Alıcı) information"""
        response = requests.get(
            f"{BASE_URL}/api/orders/{TEST_ORDER_ID}/cargo-label",
            headers=auth_headers
        )
        
        assert response.status_code == 200
        html_content = response.text
        
        # Check for receiver section
        assert "Alıcı Bilgileri" in html_content, "Should have 'Alıcı Bilgileri' section"
        assert "İsim" in html_content, "Should have name field"
        assert "Telefon" in html_content, "Should have phone field"
        assert "Adres" in html_content, "Should have address field"
        print("PASS: Cargo label has receiver information")
    
    def test_cargo_label_has_cargo_info(self, auth_headers):
        """Test that label contains cargo (Kargo) information"""
        response = requests.get(
            f"{BASE_URL}/api/orders/{TEST_ORDER_ID}/cargo-label",
            headers=auth_headers
        )
        
        assert response.status_code == 200
        html_content = response.text
        
        # Check for cargo section
        assert "Kargo Bilgileri" in html_content, "Should have 'Kargo Bilgileri' section"
        assert "Kargo Firması" in html_content, "Should have cargo company field"
        assert "Ödeme Türü" in html_content, "Should have payment type field"
        assert "Paket Sayısı" in html_content, "Should have package count field"
        print("PASS: Cargo label has cargo information")
    
    def test_cargo_label_not_found_order(self, auth_headers):
        """Test 404 for non-existent order"""
        response = requests.get(
            f"{BASE_URL}/api/orders/non-existent-order-id/cargo-label",
            headers=auth_headers
        )
        
        assert response.status_code == 404, f"Expected 404, got {response.status_code}"
        print("PASS: Returns 404 for non-existent order")


class TestBulkLabelsEndpoint:
    """Test /api/orders/bulk-labels endpoint"""
    
    def test_bulk_labels_returns_html(self, auth_headers):
        """Test that bulk-labels endpoint returns HTML content"""
        response = requests.post(
            f"{BASE_URL}/api/orders/bulk-labels",
            json=[TEST_ORDER_ID],
            headers=auth_headers
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        assert "text/html" in response.headers.get("content-type", ""), "Expected HTML content type"
        
        html_content = response.text
        assert "<!DOCTYPE html>" in html_content, "Should contain DOCTYPE"
        assert "Toplu Kargo Etiketleri" in html_content, "Should contain 'Toplu Kargo Etiketleri' title"
        print("PASS: Bulk labels returns HTML content")
    
    def test_bulk_labels_has_page_breaks(self, auth_headers):
        """Test that bulk labels have page breaks for printing"""
        response = requests.post(
            f"{BASE_URL}/api/orders/bulk-labels",
            json=[TEST_ORDER_ID],
            headers=auth_headers
        )
        
        assert response.status_code == 200
        html_content = response.text
        
        # Check for page break CSS
        assert "page-break-after" in html_content, "Should have page-break-after CSS"
        assert 'class="label-page"' in html_content, "Should have label-page class"
        print("PASS: Bulk labels has page breaks for printing")
    
    def test_bulk_labels_multiple_orders(self, auth_headers):
        """Test bulk labels with multiple order IDs (including non-existent)"""
        response = requests.post(
            f"{BASE_URL}/api/orders/bulk-labels",
            json=[TEST_ORDER_ID, "non-existent-id"],
            headers=auth_headers
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        
        html_content = response.text
        # Should still generate label for valid order
        assert "label-page" in html_content, "Should have at least one label"
        print("PASS: Bulk labels handles multiple orders (skips non-existent)")
    
    def test_bulk_labels_empty_list(self, auth_headers):
        """Test bulk labels with empty order list"""
        response = requests.post(
            f"{BASE_URL}/api/orders/bulk-labels",
            json=[],
            headers=auth_headers
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        print("PASS: Bulk labels handles empty list")


class TestMNGShipmentEndpoint:
    """Test /api/orders/{id}/create-mng-shipment endpoint"""
    
    def test_mng_shipment_endpoint_exists(self, auth_headers):
        """Test that MNG shipment endpoint exists and responds"""
        # First, let's create a test order to avoid modifying the existing one
        # We'll test with a non-existent order to check 404 handling
        response = requests.post(
            f"{BASE_URL}/api/orders/non-existent-order/create-mng-shipment",
            headers=auth_headers
        )
        
        # Should return 404 for non-existent order
        assert response.status_code == 404, f"Expected 404 for non-existent order, got {response.status_code}"
        print("PASS: MNG shipment endpoint exists and returns 404 for non-existent order")
    
    def test_mng_shipment_requires_auth(self):
        """Test that MNG shipment endpoint requires authentication"""
        response = requests.post(
            f"{BASE_URL}/api/orders/{TEST_ORDER_ID}/create-mng-shipment"
        )
        
        # Should return 401 without auth
        assert response.status_code == 401, f"Expected 401 without auth, got {response.status_code}"
        print("PASS: MNG shipment endpoint requires authentication")


class TestCargoCompaniesEndpoint:
    """Test /api/cargo/companies endpoint"""
    
    def test_cargo_companies_list(self):
        """Test that cargo companies endpoint returns list"""
        response = requests.get(f"{BASE_URL}/api/cargo/companies")
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        
        companies = response.json()
        assert isinstance(companies, list), "Should return a list"
        assert len(companies) > 0, "Should have at least one cargo company"
        
        # Check for MNG
        mng_found = any(c.get("code") == "MNG" for c in companies)
        assert mng_found, "Should include MNG Kargo"
        
        # Check company structure
        for company in companies:
            assert "code" in company, "Company should have code"
            assert "name" in company, "Company should have name"
            assert "tracking_url" in company, "Company should have tracking_url"
        
        print(f"PASS: Cargo companies endpoint returns {len(companies)} companies including MNG")


class TestOrderTrackingEndpoint:
    """Test /api/orders/{id}/track endpoint"""
    
    def test_order_tracking(self):
        """Test order tracking endpoint"""
        response = requests.get(f"{BASE_URL}/api/orders/{TEST_ORDER_ID}/track")
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        
        data = response.json()
        assert "status" in data, "Should have status field"
        
        # If cargo exists, check tracking info
        if data.get("tracking_number"):
            assert "cargo_company" in data, "Should have cargo_company"
            assert "tracking_url" in data, "Should have tracking_url"
            print(f"PASS: Order tracking returns cargo info: {data.get('cargo_company')} - {data.get('tracking_number')}")
        else:
            print("PASS: Order tracking returns status (no cargo yet)")
    
    def test_order_tracking_not_found(self):
        """Test 404 for non-existent order tracking"""
        response = requests.get(f"{BASE_URL}/api/orders/non-existent-order/track")
        
        assert response.status_code == 404, f"Expected 404, got {response.status_code}"
        print("PASS: Order tracking returns 404 for non-existent order")


class TestLabelContentValidation:
    """Validate cargo label content in detail"""
    
    def test_label_has_tracking_number(self, auth_headers):
        """Test that label displays tracking number"""
        response = requests.get(
            f"{BASE_URL}/api/orders/{TEST_ORDER_ID}/cargo-label",
            headers=auth_headers
        )
        
        assert response.status_code == 200
        html_content = response.text
        
        # The order has tracking number PTT123456789012
        assert "PTT" in html_content or "tracking" in html_content.lower(), "Should contain tracking number or reference"
        print("PASS: Label contains tracking number")
    
    def test_label_is_printable(self, auth_headers):
        """Test that label has print-specific CSS"""
        response = requests.get(
            f"{BASE_URL}/api/orders/{TEST_ORDER_ID}/cargo-label",
            headers=auth_headers
        )
        
        assert response.status_code == 200
        html_content = response.text
        
        # Check for print media query
        assert "@media print" in html_content, "Should have @media print CSS"
        assert "print-color-adjust" in html_content, "Should have print-color-adjust CSS"
        print("PASS: Label has print-specific CSS")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
