"""
Amazon Shopping via Alexa
Handles product ordering, price verification, and confirmation flows.
"""
import re
import time
import json
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ProductInfo:
    name: Optional[str] = None
    brand: Optional[str] = None
    price: Optional[float] = None
    quantity: Optional[str] = None
    raw_response: Optional[str] = None


@dataclass
class OrderResult:
    success: bool = False
    product: Optional[ProductInfo] = None
    confirmed: bool = False
    order_placed: bool = False
    error: Optional[str] = None
    transcript: list = None


class AmazonShopper:
    def __init__(self, alexa_adapter, max_price=50.0,
                 require_human_approval_above=50.0,
                 log_file=None):
        """
        Args:
            alexa_adapter: AlexaAdapter instance
            max_price: Auto-reject products above this price
            require_human_approval_above: Require human OK above this price
            log_file: Path to purchase audit log
        """
        self.alexa = alexa_adapter
        self.max_price = max_price
        self.approval_threshold = require_human_approval_above
        self.log_file = log_file

    def order(self, product_name, max_price=None, auto_confirm=False):
        """
        Order a product via Alexa.

        Args:
            product_name: What to order (e.g., "windshield washer fluid")
            max_price: Override max price for this order
            auto_confirm: Skip price check and confirm immediately

        Returns:
            OrderResult
        """
        max_price = max_price or self.max_price
        result = OrderResult(transcript=[])

        # Step 1: Ask Alexa to order
        logger.info(f"Ordering: {product_name}")
        response = self.alexa.command(f"order {product_name}")

        if not response:
            result.error = "No response from Alexa"
            self._log_attempt(product_name, result)
            return result

        # Step 2: Parse what Alexa offered
        product = self._parse_product_response(response)
        result.product = product

        if not product.price:
            # Alexa might have said something unexpected
            result.error = f"Could not parse product/price from: {response}"
            # Say no to cancel
            self.alexa.deny()
            self._log_attempt(product_name, result)
            return result

        # Step 3: Price check
        if product.price > max_price:
            result.error = f"Price ${product.price:.2f} exceeds max ${max_price:.2f}"
            self.alexa.deny()
            logger.info(f"Rejected: {result.error}")
            self._log_attempt(product_name, result)
            return result

        # Step 4: Confirm or deny
        if auto_confirm or product.price <= self.approval_threshold:
            confirm_response = self.alexa.confirm()
            result.confirmed = True

            if confirm_response and self._is_order_confirmed(confirm_response):
                result.success = True
                result.order_placed = True
                logger.info(f"Order placed: {product.name} @ ${product.price:.2f}")
            else:
                result.error = f"Confirmation unclear: {confirm_response}"
        else:
            # Need human approval
            result.error = f"Price ${product.price:.2f} requires human approval"
            self.alexa.deny()

        result.transcript = self.alexa.get_transcript()
        self._log_attempt(product_name, result)
        return result

    def _parse_product_response(self, response):
        """
        Parse Alexa's product suggestion.

        Examples:
            "I found Rain-X Windshield Washer Fluid, 1 gallon for $4.97"
            "I found Bounty Paper Towels, 8 rolls for $12.99. Should I order it?"
            "The top result is Clorox Wipes for $6.48. Want me to buy it?"
        """
        product = ProductInfo(raw_response=response)

        # Extract price
        price_match = re.search(r'\$(\d+\.?\d*)', response)
        if price_match:
            product.price = float(price_match.group(1))

        # Extract product name (text between "found"/"result is" and price)
        name_patterns = [
            r'(?:I found|the top result is|how about)\s+(.+?)(?:\s+for\s+\$)',
            r'(?:I found|the top result is)\s+(.+?)(?:,?\s*(?:for|at)\s+\$)',
            r'(?:order|buy)\s+(.+?)(?:\s+for\s+\$)',
        ]
        for pattern in name_patterns:
            name_match = re.search(pattern, response, re.IGNORECASE)
            if name_match:
                product.name = name_match.group(1).strip().rstrip(',')
                break

        if not product.name and response:
            # Fallback: use everything before the price
            if price_match:
                product.name = response[:price_match.start()].strip().rstrip(',. ')

        return product

    def _is_order_confirmed(self, response):
        """Check if Alexa confirmed the order was placed."""
        confirmation_phrases = [
            "placed your order",
            "order has been placed",
            "on its way",
            "ordered",
            "i've placed",
            "your order for",
            "arriving",
        ]
        response_lower = response.lower()
        return any(phrase in response_lower for phrase in confirmation_phrases)

    def check_order_status(self, product_name=None):
        """Ask Alexa about recent order status."""
        if product_name:
            return self.alexa.ask(f"where's my order for {product_name}")
        return self.alexa.ask("where's my order")

    def add_to_list(self, item):
        """Add item to Alexa shopping list."""
        return self.alexa.command(f"add {item} to my shopping list")

    def _log_attempt(self, product_name, result):
        """Log purchase attempt for audit trail."""
        if not self.log_file:
            return

        entry = {
            'timestamp': time.time(),
            'product_requested': product_name,
            'product_offered': result.product.name if result.product else None,
            'price': result.product.price if result.product else None,
            'success': result.success,
            'confirmed': result.confirmed,
            'error': result.error,
        }

        try:
            with open(self.log_file, 'a') as f:
                f.write(json.dumps(entry) + '\n')
        except Exception as e:
            logger.error(f"Failed to log purchase attempt: {e}")


if __name__ == '__main__':
    # Test response parsing
    shopper = AmazonShopper(alexa_adapter=None)
    test_responses = [
        "I found Rain-X Windshield Washer Fluid, 1 gallon for $4.97. Should I order it?",
        "The top result is Bounty Paper Towels, 8 rolls for $12.99. Want me to buy it?",
        "I found Kleenex Ultra Soft Facial Tissues for $2.49. Shall I order it?",
    ]
    print("Testing response parser:")
    for resp in test_responses:
        product = shopper._parse_product_response(resp)
        print(f"  Input: {resp}")
        print(f"  Name: {product.name} | Price: ${product.price}")
        print()
