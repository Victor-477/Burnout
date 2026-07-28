/* ============================================================
 *  LibPyro — Modern C++ Embedding Header (Header-only)
 * ============================================================ */
#ifndef PYRO_HPP
#define PYRO_HPP

#include <string>
#include <vector>
#include <functional>
#include <stdexcept>
#include "../../c_api/include/pyro_embed.h"

namespace pyro {

class Value {
public:
    pyro_value_t raw;

    Value() { raw = pyro_make_null(); }
    Value(bool b) { raw = pyro_make_bool(b); }
    Value(int i) { raw = pyro_make_int(i); }
    Value(int64_t i) { raw = pyro_make_int(i); }
    Value(double f) { raw = pyro_make_float(f); }
    Value(const std::string& s) { raw = pyro_make_string(s.c_str()); }
    Value(const char* s) { raw = pyro_make_string(s); }

    ~Value() {
        pyro_value_free(raw);
    }

    Value(const Value& other) {
        if (other.raw.type == PYRO_TYPE_STRING || other.raw.type == PYRO_TYPE_ERROR) {
            raw = pyro_make_string(other.raw.as.s);
        } else {
            raw = other.raw;
        }
    }

    Value& operator=(const Value& other) {
        if (this != &other) {
            pyro_value_free(raw);
            if (other.raw.type == PYRO_TYPE_STRING || other.raw.type == PYRO_TYPE_ERROR) {
                raw = pyro_make_string(other.raw.as.s);
            } else {
                raw = other.raw;
            }
        }
        return *this;
    }

    pyro_type_t type() const { return raw.type; }
    bool is_null() const { return raw.type == PYRO_TYPE_NULL; }
    bool as_bool() const { return pyro_get_bool(raw); }
    int64_t as_int() const { return pyro_get_int(raw); }
    double as_float() const { return pyro_get_float(raw); }
    std::string as_string() const { return pyro_get_string(raw); }
};

class VM {
private:
    pyro_vm_t* handle_;

public:
    VM(bool sandbox = false, bool debug = false) {
        pyro_config_t cfg = { sandbox, debug };
        handle_ = pyro_vm_create(&cfg);
        if (!handle_) {
            throw std::runtime_error("Failed to create Pyro VM instance");
        }
    }

    ~VM() {
        if (handle_) {
            pyro_vm_free(handle_);
            handle_ = nullptr;
        }
    }

    VM(const VM&) = delete;
    VM& operator=(const VM&) = delete;

    void load_bytecode(const std::vector<uint8_t>& bytes) {
        if (pyro_vm_load_bytecode(handle_, bytes.data(), bytes.size()) != 0) {
            throw std::runtime_error(pyro_vm_get_error(handle_));
        }
    }

    Value eval(const std::string& source) {
        pyro_value_t res;
        if (pyro_vm_eval(handle_, source.c_str(), &res) != 0) {
            throw std::runtime_error(pyro_vm_get_error(handle_));
        }
        Value v;
        v.raw = res;
        return v;
    }

    Value call(const std::string& func_name, const std::vector<Value>& args) {
        std::vector<pyro_value_t> raw_args;
        for (const auto& a : args) {
            raw_args.push_back(a.raw);
        }
        pyro_value_t res;
        if (pyro_vm_call(handle_, func_name.c_str(), raw_args.data(), raw_args.size(), &res) != 0) {
            throw std::runtime_error(pyro_vm_get_error(handle_));
        }
        Value v;
        v.raw = res;
        return v;
    }
};

} // namespace pyro

#endif /* PYRO_HPP */
