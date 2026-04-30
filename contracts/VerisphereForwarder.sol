// SPDX-License-Identifier: BUSL-1.1
// Copyright (c) 2025 Verisphere Ltd. All rights reserved.
//
// This contract is a COMMERCIAL SERVICE COMPONENT operated by Verisphere Ltd.
// It is NOT part of the VeriSphere open-source protocol (verisphere/core).
//
// License: Business Source License 1.1 (BUSL-1.1)
// Change Date: 2028-01-01
// Change License: MIT

pragma solidity ^0.8.20;

import "@openzeppelin/contracts/metatx/ERC2771Forwarder.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/proxy/utils/UUPSUpgradeable.sol";

/// @title VerisphereForwarder (Upgradeable)
/// @notice Trusted forwarder for gasless meta-transactions (ERC-2771) with
///         a percentage-based VSP relay fee. Deployed behind an ERC1967 proxy.
///
///         The protocol does not require this forwarder. Users may call
///         protocol contracts directly with their own wallet and gas.
contract VerisphereForwarder is ERC2771Forwarder, UUPSUpgradeable {

    // ── State (stored in proxy) ──────────────────────────────

    IERC20 public vspToken;
    address public treasury;
    address public owner;
    uint256 public feeBps;
    uint256 public minFeeWei;
    bool public feeEnabled;
    bool private _initialized;

    // Known function selectors for value extraction
    bytes4 private constant SEL_CREATE_CLAIM = bytes4(keccak256("createClaim(string)"));
    bytes4 private constant SEL_CREATE_LINK  = bytes4(keccak256("createLink(uint256,uint256,bool)"));
    bytes4 private constant SEL_STAKE        = bytes4(keccak256("stake(uint256,uint8,uint256)"));
    bytes4 private constant SEL_WITHDRAW     = bytes4(keccak256("withdraw(uint256,uint8,uint256,bool)"));

    // ── Events ───────────────────────────────────────────────

    event FeeCollected(address indexed user, uint256 fee, uint256 txValue);
    event FeeConfigUpdated(uint256 feeBps, uint256 minFeeWei, bool enabled);
    event TreasuryUpdated(address indexed oldTreasury, address indexed newTreasury);
    event OwnerUpdated(address indexed oldOwner, address indexed newOwner);

    // ── Errors ───────────────────────────────────────────────

    error NotOwner();
    error ZeroAddress();
    error AlreadyInitialized();

    // ── Constructor (implementation only) ────────────────────

    /// @dev Constructor sets up EIP-712 domain for the implementation.
    ///      When called via proxy, EIP712._domainSeparatorV4() detects the
    ///      address mismatch and recomputes using the proxy address.
    constructor() ERC2771Forwarder("VerisphereForwarder") {}

    // ── Initializer (called once on proxy) ───────────────────

    /// @notice Initialize the proxy with forwarder configuration.
    ///         Called once during proxy deployment via ERC1967Proxy constructor.
    function initialize(
        address vspToken_,
        address treasury_,
        address owner_,
        uint256 feeBps_,
        uint256 minFeeWei_
    ) external {
        if (_initialized) revert AlreadyInitialized();
        if (vspToken_ == address(0)) revert ZeroAddress();
        if (treasury_ == address(0)) revert ZeroAddress();
        if (owner_ == address(0)) revert ZeroAddress();
        _initialized = true;
        vspToken = IERC20(vspToken_);
        treasury = treasury_;
        owner = owner_;
        feeBps = feeBps_;
        minFeeWei = minFeeWei_;
        feeEnabled = true;
    }

    // ── UUPS authorization ───────────────────────────────────

    function _authorizeUpgrade(address) internal view override {
        if (msg.sender != owner) revert NotOwner();
    }

    // ── Modifiers ────────────────────────────────────────────

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    // ── Fee extraction ───────────────────────────────────────

    function _extractTxValue(bytes calldata data) internal pure returns (uint256) {
        if (data.length < 4) return 0;
        bytes4 sel = bytes4(data[:4]);

        if (sel == SEL_CREATE_CLAIM || sel == SEL_CREATE_LINK) {
            return 1e18;
        }
        if (sel == SEL_STAKE) {
            if (data.length >= 100) {
                return uint256(bytes32(data[68:100]));
            }
            return 0;
        }
        if (sel == SEL_WITHDRAW) {
            if (data.length >= 100) {
                return uint256(bytes32(data[68:100]));
            }
            return 0;
        }
        return 0;
    }

    function _collectFee(address user, bytes calldata innerData) internal {
        if (!feeEnabled || feeBps == 0) return;

        uint256 txValue = _extractTxValue(innerData);
        uint256 fee = (txValue * feeBps) / 10_000;
        if (fee < minFeeWei) fee = minFeeWei;

        require(
            vspToken.transferFrom(user, treasury, fee),
            "Relay fee: insufficient VSP balance or allowance"
        );
        emit FeeCollected(user, fee, txValue);
    }

    function estimateFee(bytes calldata innerData) external view returns (uint256) {
        if (!feeEnabled || feeBps == 0) return 0;
        uint256 txValue = _extractTxValue(innerData);
        uint256 fee = (txValue * feeBps) / 10_000;
        if (fee < minFeeWei) fee = minFeeWei;
        return fee;
    }

    // ── Execute override ─────────────────────────────────────

    function execute(
        ForwardRequestData calldata request
    ) public payable override {
        _collectFee(request.from, request.data);
        super.execute(request);
    }

    // ── Admin ────────────────────────────────────────────────

    function setFeeConfig(
        uint256 feeBps_,
        uint256 minFeeWei_,
        bool enabled_
    ) external onlyOwner {
        feeBps = feeBps_;
        minFeeWei = minFeeWei_;
        feeEnabled = enabled_;
        emit FeeConfigUpdated(feeBps_, minFeeWei_, enabled_);
    }

    function setTreasury(address treasury_) external onlyOwner {
        if (treasury_ == address(0)) revert ZeroAddress();
        address old = treasury;
        treasury = treasury_;
        emit TreasuryUpdated(old, treasury_);
    }

    function setOwner(address newOwner) external onlyOwner {
        if (newOwner == address(0)) revert ZeroAddress();
        address old = owner;
        owner = newOwner;
        emit OwnerUpdated(old, newOwner);
    }
}
