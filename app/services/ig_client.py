import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, Union

import requests
from dotenv import load_dotenv

from app.models.enums import OrderDirection, OrderType
from config.settings import HEDGE_SETTINGS as _hedge_settings

load_dotenv()
logger = logging.getLogger(__name__)

class IGAPIError(Exception):
    def __init__(self, message, status_code=None, response_text=None):
        self.message = message
        self.status_code = status_code
        self.response_text = response_text
        super().__init__(self.message)

    def __str__(self):
        error_msg = self.message
        if self.status_code:
            error_msg += f" (Status: {self.status_code})"
        if self.response_text:
            error_msg += f" Response: {self.response_text}"
        return error_msg

class IGClient:
    def __init__(self, api_key, username, password):
        """Initialize IGClient with configuration"""
        # API credentials
        self.api_key = api_key
        self.username = username
        self.password = password
        self.base_url = "https://demo-api.ig.com/gateway/deal"
        self.session = requests.Session()
        self.account_id = None
        self.access_token = None
        self.refresh_token = None
        self.token_expiry = None

        # Rate limiting
        self.request_delay = 1.0
        self.last_request_time = 0
        self.max_retries = 3
        self.retry_delay = 2
        self.request_interval = float(_hedge_settings.get("api_request_interval", 1.0))
        
        self.login()

    def _validate_credentials(self) -> None:
        """Validate API credentials"""
        missing = []
        if not self.api_key:
            missing.append("IG_API_KEY")
        if not self.username:
            missing.append("IG_USERNAME")
        if not self.password:
            missing.append("IG_PASSWORD")

        if missing:
            raise ValueError(f"Missing IG API credentials: {', '.join(missing)}")

    def refresh_access_token(self) -> bool:
        """Refresh the access token using the refresh token"""
        if not self.refresh_token:
            logger.error("No refresh token available")
            return False

        headers = {
            "X-IG-API-KEY": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json; charset=UTF-8",
            "Version": "1"
        }

        try:
            response = self.session.post(
                f"{self.base_url}/session/refresh-token",
                headers=headers,
                json={"refresh_token": self.refresh_token}
            )

            if response.status_code == 200:
                response_data = response.json()
                self.access_token = response_data.get('access_token')
                self.refresh_token = response_data.get('refresh_token')
                expires_in = int(response_data.get('expires_in', 0))
                self.token_expiry = datetime.now() + timedelta(seconds=expires_in)
                logger.info("Successfully refreshed access token")
                return True
            else:
                logger.error(f"Failed to refresh token: {response.status_code}")
                return False
        except Exception as e:
            logger.error(f"Error refreshing token: {str(e)}")
            return False


    def _handle_rate_limit(self, response: requests.Response) -> bool:
        """Handle rate limiting errors"""
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 60))
            logger.warning(f"Rate limit exceeded, waiting {retry_after} seconds")
            time.sleep(retry_after)
            return True

        error_code = response.json().get("errorCode", "")
        if (
            "exceeded-api-key-allowance" in error_code
            or "exceeded-account-allowance" in error_code
        ):
            logger.warning(f"API limit exceeded: {error_code}")
            time.sleep(60)  # Wait for 1 minute
            return True

        return False

    def _handle_response(self, response: requests.Response, operation: str) -> Dict:
        """Handle API response with rate limiting"""
        try:
            if self._handle_rate_limit(response):
                return {"error": "Rate limit exceeded, please try again"}

            if response.status_code in [200, 201]:
                return response.json()

            error_msg = f"{operation} failed with status {response.status_code}: {response.text}"
            logger.error(error_msg)
            return {"error": error_msg}

        except Exception as e:
            logger.error(f"Error handling response for {operation}: {str(e)}")
            return {"error": str(e)}
        
    def ensure_token_valid(self):
        """Ensure the access token is valid, refresh if needed"""
        if not self.token_expiry or not self.access_token:
            raise IGAPIError("No valid session - needs new login")

        # Refresh if token expires in less than 15 seconds
        if datetime.now() + timedelta(seconds=15) >= self.token_expiry:
            logger.info("Token expiring soon, attempting refresh...")
            success = self.refresh_access_token()
            if not success:
                self.access_token = None
                self.refresh_token = None
                self.token_expiry = None
                raise IGAPIError("Session expired - needs new login")

    def login(self):
        """Authenticate with the IG API"""
        
        try:
            self._validate_credentials()
            
            headers = {
                "X-IG-API-KEY": self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json; charset=UTF-8",
                "Version": "3"
            }

            response = self.session.post(
                f"{self.base_url}/session",
                headers=headers,
                json={"identifier": self.username, "password": self.password}
            )

            logger.info(f"Login response status: {response.status_code}")
            logger.info(f"Login response body: {response.text}")

            if response.status_code == 200:
                response_data = response.json()
                self.account_id = response_data.get('accountId')
                oauth_token = response_data.get('oauthToken', {})
                self.access_token = oauth_token.get('access_token')
                self.refresh_token = oauth_token.get('refresh_token')
                expires_in = int(oauth_token.get('expires_in', 0))
                self.token_expiry = datetime.now() + timedelta(seconds=expires_in)
                logger.info("Successfully authenticated with IG API")
                return True

            elif response.status_code == 401:
                logger.error("Authentication failed - invalid credentials")
                raise IGAPIError("Invalid credentials provided")
            else:
                logger.error(f"Authentication failed with status code: {response.status_code}")
                raise IGAPIError("Failed to authenticate with IG API")

        except requests.exceptions.RequestException as e:
            logger.error(f"Network error during authentication: {str(e)}")
            raise IGAPIError("Network error occurred while connecting to IG API")
        except Exception as e:
            logger.error(f"Unexpected error during authentication: {str(e)}")
            raise IGAPIError("An unexpected error occurred during authentication")

    def get_headers(self, version: str = "2") -> Dict:
        """Get headers for API requests"""
        self.ensure_token_valid()
        
        return {
            "X-IG-API-KEY": self.api_key,
            "Authorization": f"Bearer {self.access_token}",
            "IG-ACCOUNT-ID": self.account_id,
            "Content-Type": "application/json",
            "Accept": "application/json; charset=UTF-8",
            "Version": version
        }

    def get_positions(self) -> Dict:
        """Fetch current positions"""
        try:
            # First ensure token is valid
            self.ensure_token_valid()
            if not self.access_token:
                raise IGAPIError("Not authenticated - please log in first")

            # Make the request
            response = self.session.get(
                f"{self.base_url}/positions",
                headers=self.get_headers(version="1"),  # Note we're using version 1 here
                timeout=30
            )

            logger.debug(f"Position response status: {response.status_code}")
            logger.debug(f"Position response text: {response.text}")

            if response.status_code == 200:
                logger.info("Successfully fetched positions")
                return response.json()
            elif response.status_code == 401:
                # Clear tokens and raise error
                self.access_token = None
                self.refresh_token = None
                self.token_expiry = None
                raise IGAPIError(
                    message="Session expired - needs new login",
                    status_code=response.status_code,
                    response_text=response.text
                )
            else:
                logger.error(f"Failed to fetch positions: HTTP {response.status_code}")
                raise IGAPIError(
                    message="Failed to fetch positions from IG API",
                    status_code=response.status_code,
                    response_text=response.text
                )

        except requests.exceptions.RequestException as e:
            logger.error(f"Network error while fetching positions: {str(e)}")
            raise IGAPIError(f"Network error occurred while fetching positions: {str(e)}")
        except IGAPIError:
            raise  # Re-raise IGAPIError as is
        except Exception as e:
            logger.error(f"Unexpected error while fetching positions: {str(e)}")
            raise IGAPIError(f"An unexpected error occurred while fetching positions: {str(e)}")
    
    def get_market_data(self, epic: str) -> Dict:
        """
        Fetch details for a specific market by its epic code

        Args:
            epic: The epic identifier for the market

        Returns:
            dict: Market details including price, volatility, etc.
        """
        try:
            self._rate_limit()
            self.ensure_token_valid()
            
            if not self.access_token:
                raise IGAPIError("Not authenticated - please log in first")

            response = self.session.get(
                f"{self.base_url}/markets/{epic}",
                headers=self.get_headers(version="3")
            )

            if response.status_code == 200:
                data = response.json()
                snapshot = data.get("snapshot", {})
                instrument = data.get("instrument", {})

                # Process the data like in the original
                market_data = {
                    "bid": float(snapshot.get("bid", 0)),
                    "offer": float(snapshot.get("offer", 0)),
                    "price": (float(snapshot.get("bid", 0)) + float(snapshot.get("offer", 0))) / 2,
                    "high": float(snapshot.get("high", 0)),
                    "low": float(snapshot.get("low", 0)),
                    "update_time": snapshot.get("updateTime"),
                    "volatility": max(0.001, abs(float(snapshot.get("percentageChange", 0.1)) / 100)),
                    "instrument_type": instrument.get("type", ""),
                    "market_status": snapshot.get("marketStatus", ""),
                    "strike_price": float(instrument.get("strikePrice", 0)),
                    "expiry": instrument.get("expiry", ""),
                }
                
                logger.info(f"Successfully fetched market data for {epic}")
                return market_data
                
            elif response.status_code == 401:
                raise IGAPIError("Session expired - please log in again")
            else:
                raise IGAPIError(f"Failed to fetch market details from IG API: HTTP {response.status_code}")
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Network error while fetching market details: {str(e)}")
            raise IGAPIError(f"Network error occurred while fetching market details: {str(e)}")
        except IGAPIError:
            raise
        except Exception as e:
            logger.error(f"Unexpected error while fetching market details: {str(e)}")
            raise IGAPIError(f"An unexpected error occurred while fetching market details: {str(e)}")

    def create_position(
        self,
        epic: str,
        direction: OrderDirection,
        size: float,
        order_type: OrderType = OrderType.MARKET,
        limit_level: Optional[float] = None,
        time_in_force: str = "EXECUTE_AND_ELIMINATE",
    ) -> Dict:
        try:
            # Determine the CFD account ID
            cfd_account_id = os.getenv("IG_CFD_ACCOUNT", self.account_id)

            logger.info(f"Using account ID for position creation: {cfd_account_id}")

            # Robust size validation and formatting
            try:
                # Ensure size is a positive float
                size = float(abs(size))  # Take absolute value to handle negative inputs

                # Apply minimum size constraint
                min_trade_size = 0.01  # Minimum trade size
                size = max(size, min_trade_size)

                # Ensure size is formatted as a string with 2 decimal places
                formatted_size = f"{size:.2f}"
            except (ValueError, TypeError) as size_error:
                logger.error(f"Invalid size input: {size}, Error: {str(size_error)}")
                return {
                    "error": "Position size must be positive",
                    "details": {"original_size": size, "error": str(size_error)},
                }

            # Get market data for price validation
            market_data = self.get_market_data(epic)
            if not market_data:
                return {"error": "Failed to get market data"}

            current_price = market_data.get("price", 0)
            if current_price <= 0:
                return {"error": "Invalid market price"}

            # Base order parameters
            base_order = {
                "epic": epic,
                "expiry": "-",
                "direction": direction.value,
                "size": formatted_size,  # Use formatted size
                "orderType": order_type.value,
                "currencyCode": "GBP",  # Required field
                "forceOpen": True,  # Required for limit orders
                "guaranteedStop": False,
                "timeInForce": time_in_force,
            }

            # Handle different order types
            if order_type == OrderType.MARKET:
                base_order.pop("level", None)
                base_order.pop("quoteId", None)
            elif order_type == OrderType.LIMIT:
                price_level = limit_level or current_price
                base_order["level"] = str(price_level)
                base_order.pop("quoteId", None)
            elif order_type == OrderType.QUOTE:  # type: ignore
                return {"error": "Quote orders not supported"}

            logger.info(f"Sending order: {base_order}")
            response = self.session.post(
                f"{self.base_url}/positions/otc",
                headers=self.get_headers(),
                json=base_order,
                timeout=30,
            )

            # Detailed response handling
            if response.status_code == 200:
                result = response.json()
                logger.info(f"Order created successfully: {result}")

                if "dealReference" in result:
                    return {
                        "dealId": result["dealReference"],
                        "dealReference": result["dealReference"],
                    }

                logger.error(f"No dealReference found in successful response: {result}")
                return {
                    "error": "Position creation succeeded but no deal reference found",
                    "raw_result": result,
                }

            # Detailed error parsing
            try:
                error_data = response.json()
                error_code = error_data.get("errorCode", "Unknown error")
                error_details = {
                    "status_code": response.status_code,
                    "error_code": error_code,
                    "raw_response": response.text,
                    "order_details": base_order,
                }
                logger.error(
                    f"Order creation failed: {json.dumps(error_details, indent=2)}"
                )
                return {
                    "error": f"Position creation failed: {error_code}",
                    "details": error_details,
                }
            except Exception as parsing_error:
                logger.error(f"Error parsing error response: {str(parsing_error)}")
                return {
                    "error": f"Position creation failed with status {response.status_code}",
                    "raw_response": response.text,
                }

        except Exception as e:
            logger.error(f"Unexpected error creating position: {str(e)}", exc_info=True)
            return {"error": "Position creation failed", "details": str(e)}

    def _parse_error_response(self, response: requests.Response) -> str:
        """Parse error response from IG API"""
        try:
            error_data = response.json()
            if "errorCode" in error_data:
                return error_data["errorCode"]
            elif "error" in error_data:
                return error_data["error"]
            else:
                return f"HTTP {response.status_code}: {response.text}"
        except Exception:
            return f"HTTP {response.status_code}: {response.text}"

    def _rate_limit(self) -> None:
        """Implement rate limiting"""
        current_time = time.time()
        elapsed = current_time - self.last_request_time

        if elapsed < self.request_interval:
            time.sleep(self.request_interval - elapsed)

        self.last_request_time = time.time()

    def create_hedge_position(
        self, epic: str, direction: OrderDirection, size: float
    ) -> Dict:
        """Create a CFD hedge position with robust account switching"""
        try:
            # Logging the original epic and input parameters for debugging
            logger.info(f"Creating hedge position:")
            logger.info(f"Original epic: {epic}")
            logger.info(f"Direction: {direction}")
            logger.info(f"Size: {size}")

            epic = "IX.D.SPTRD.IFS.IP"
            # Ensure we're using the CFD account
            cfd_account_id = os.getenv("IG_CFD_ACCOUNT")
            if not cfd_account_id:
                return {"error": "CFD account ID not configured"}

            # Temporarily switch to CFD account
            try:
                # Login with CFD account
                if not self.login(account_type="cfd"):
                    return {"error": "Failed to authenticate CFD account"}

                # Create CFD position
                result = self.create_position(
                    epic=epic,
                    direction=direction,
                    size=size,
                    order_type=OrderType.MARKET,
                )

                # Switch back to options account
                self.login(account_type="options")

                # Log the result
                if "dealId" in result or "dealReference" in result:
                    logger.info(f"Hedge position created successfully: {result}")
                    return result
                else:
                    logger.error(f"Failed to create hedge position: {result}")
                    return {
                        "error": "Failed to create hedge position",
                        "details": result,
                    }

            except Exception as e:
                # Ensure we switch back to options account even if an error occurs
                try:
                    self.login(account_type="options")
                except Exception:
                    logger.error(
                        "Failed to switch back to options account after hedge error"
                    )

                logger.error(f"Comprehensive error creating hedge position: {str(e)}")
                return {
                    "error": "Comprehensive hedge position creation error",
                    "details": str(e),
                }

        except Exception as e:
            logger.error(
                f"Unexpected error creating hedge position: {str(e)}", exc_info=True
            )
            return {"error": "Hedge position creation failed", "details": str(e)}
