// Globals to track login state
let loggedInAsAdmin = false;

function showAdminSection() {
  if (loggedInAsAdmin) {
    document.getElementById("adminAddDoctorDiv").style.display = "block";
    document.getElementById("adminLoginDiv").style.display = "none";
  } else {
    document.getElementById("adminAddDoctorDiv").style.display = "none";
    document.getElementById("adminLoginDiv").style.display = "block";
  }
}

async function loadDoctors() {
  const resp = await fetch("/get_doctors");
  const doctors = await resp.json();
  const doctorSelect = document.getElementById("doctorSelect");
  const specSet = new Set();
  const specSelect = document.getElementById("specSelect");

  doctorSelect.innerHTML = "<option value=''>-- Select Doctor --</option>";
  specSelect.innerHTML = "<option value=''>-- Select Specialization --</option>";

  doctors.forEach(d => {
    let opt = document.createElement("option");
    opt.value = d.SheetName;
    opt.textContent = `${d.Name} (${d.Specialization})`;
    doctorSelect.appendChild(opt);
    specSet.add(d.Specialization);
  });

  Array.from(specSet).sort().forEach(spec => {
    let opt = document.createElement("option");
    opt.value = spec;
    opt.textContent = spec;
    specSelect.appendChild(opt);
  });
}

// Admin login form
document.getElementById("adminLoginForm").addEventListener("submit", async e => {
  e.preventDefault();
  const email = document.getElementById("adminEmail").value.trim();
  const res = await fetch("/admin_login", {
    method: "POST",
    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: `admin_email=${encodeURIComponent(email)}`
  });
  const data = await res.json();
  if (data.success) {
    alert("Admin login successful");
    loggedInAsAdmin = true;
    showAdminSection();
    loadDoctors();
  } else {
    document.getElementById("adminLoginResult").textContent = data.msg || "Login failed";
  }
});

// Admin logout button
document.getElementById("logoutBtn").addEventListener("click", async () => {
  await fetch("/admin_logout");
  loggedInAsAdmin = false;
  alert("Logged out");
  showAdminSection();
  loadDoctors();
});

// Admin add doctor form
document.getElementById("adminAddDoctorForm").addEventListener("submit", async e => {
  e.preventDefault();
  const name = document.getElementById("addDocName").value.trim();
  const spec = document.getElementById("addDocSpec").value.trim();
  const time = document.getElementById("addDocTime").value.trim();
  const daysElems = document.querySelectorAll('input[name="workDays"]:checked');
  const days = Array.from(daysElems).map(d => d.value);

  if (!name || !spec || !time || days.length === 0) {
    alert("Please fill all fields and select at least one working day.");
    return;
  }

  const res = await fetch("/admin_add_doctor", {
    method: "POST",
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name, specialization: spec, days, time})
  });
  const data = await res.json();
  if (data.success) {
    alert(data.msg);
    document.getElementById("adminAddDoctorForm").reset();
    loadDoctors();
  } else {
    alert(data.msg || "Failed to add doctor.");
  }
});

// Book by doctor form
document.getElementById("doctorBookingForm").addEventListener("submit", async e => {
  e.preventDefault();
  const sheetname = document.getElementById("doctorSelect").value;
  const name = document.getElementById("docName").value.trim();
  const age = document.getElementById("docAge").value.trim();
  const email = document.getElementById("docEmail").value.trim();
  const date = document.getElementById("doctorDate").value;

  if (!sheetname || !date) {
    alert("Please select doctor and date.");
    return;
  }
  const res = await fetch("/book_doctor", {
    method: "POST",
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({sheetname, name, age, email, date})
  });
  const data = await res.json();
  if (data.success) {
    document.getElementById("doctorBookingResult").textContent = `Booked! Your token number is ${data.token} with Dr. ${data.doctor} on ${data.date}.`;
    document.getElementById("doctorBookingForm").reset();
  } else {
    document.getElementById("doctorBookingResult").textContent = `Error: ${data.msg}`;
  }
});

// Book by department form
document.getElementById("departmentBookingForm").addEventListener("submit", async e => {
  e.preventDefault();
  const specialization = document.getElementById("specSelect").value;
  const name = document.getElementById("depName").value.trim();
  const age = document.getElementById("depAge").value.trim();
  const email = document.getElementById("depEmail").value.trim();
  const date = document.getElementById("specDate").value;

  if (!specialization || !date) {
    alert("Please select specialization and date.");
    return;
  }

  const res = await fetch("/book_department", {
    method: "POST",
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({specialization, name, age, email, date})
  });
  const data = await res.json();
  if (data.success) {
    document.getElementById("departmentBookingResult").textContent = `Booked! Your token number is ${data.token} with Dr. ${data.doctor} on ${data.date}.`;
    document.getElementById("departmentBookingForm").reset();
  } else {
    document.getElementById("departmentBookingResult").textContent = `Error: ${data.msg}`;
  }
});

window.onload = () => {
  showAdminSection();
  loadDoctors();
};
