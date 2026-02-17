import { useState, useEffect } from 'react';
import axios from 'axios';
import './App.css';
import BranchSelect from './Components/BranchSelect';
import { useImmer } from 'use-immer';
import CompositionEditor from './Components/CompositionEditor';
import CompositionInfo from './Components/CompositionInfo';
import ScalarFields from './Components/ScalarFields';
import UnitedAtomDictEditor from './Components/UnitedAtomDictEditor';
import CreateInfoFile from './Utils/CreateInfoFile';
import fieldConfig, { dropdownOptions } from './Components/FieldConfig';
import getInitialData  from './Utils/Data';
import Information from './Components/Information';

export default function App() {
  const OAUTH_ClientID = process.env.REACT_APP_OAUTH_CLIENT_ID;
  const DataRepo = process.env.REACT_APP_DATA_REPO;
  const GITHUB_GATEWAY_PATH = '/app/';
  const API_PATH = '/api/';
  const [loggedIn, setLoggedIn] = useState(false);
  const [showInformation, setShowInformation] = useState(false);
  const [adminStatus, setAdminStatus] = useState(
  localStorage.getItem('adminStatus') === 'true'
);
  const [loggedInMessage, setLoggedInMessage] = useState(null);
  const [userName, setUserName] = useState('');
  const [branch, setBranch] = useState('main');
  const [message, setMessage] = useState('Fill in the form');
  const [pullRequestUrl, setPullRequestUrl] = useState(null);
  const [refreshMessage, setRefreshMessage] = useState('');
  const [lipidList, setLipidList] = useState([]);
  const [solutionList, setSolutionList] = useState([]);
  const [uploadStatus, setUploadStatus] = useState(null);
  const [mappingDict,setMappingDict] = useState(
  JSON.parse(localStorage.getItem('mappingDict')) || {});
  const [data,setData] = useImmer(getInitialData());
  const resetData = () => setData(getInitialData());
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [lastSavedFingerprint, setLastSavedFingerprint] = useState('');
  // Fetch the up‐to‐date molecule list on mount:
 useEffect(() => {
    axios.get(`${API_PATH}molecules`)
      .then(res => {
      setLipidList(res.data.lipids)
      setSolutionList(res.data.solution)
     })
      .catch(err => console.error("Failed to load molecules:", err));
  }, []);
// Fetch mapping files on mount
useEffect(() => {
  axios.get(`${API_PATH}mapping-files`)
    .then(res => {
      localStorage.setItem('mappingDict', JSON.stringify(res.data)); 
    })
    .catch(err => console.error("Failed to load mappings:", err));
}, []);  

// Function to update databank files:
const updateDatabankFiles = async () => {
  try {
    await axios.post(
      `${API_PATH}refresh-databank-files`,
      {},
      { headers: { Authorization: `Bearer ${localStorage.githubToken}` } }
    );
    setRefreshMessage('Databank files updated successfully');
    // re-fetch updated lists
    const resp = await axios.get(`${API_PATH}molecules`);
    setLipidList(resp.data.lipids);
    setSolutionList(resp.data.solution);

    // re-fetch mapping file lists
   axios.get(`${API_PATH}mapping-files`)
    .then(res => {
      localStorage.setItem('mappingDict', JSON.stringify(res.data));
      setMappingDict(res.data);   
    })
    .catch(err => console.error("Failed to load mappings:", err));  
  } catch (err) {
    if (err.response?.status === 403) {
      setRefreshMessage('Not authorized to refresh databank files');
    } else {
      setRefreshMessage('Refresh failed');
    }
  }
}; 

// Method to handle changes in form fields:
const handleChange = e => {
  const { name, value } = e.target;
  const { type, trim } = fieldConfig[name];
  let val = value;
  if (trim) val = val.trim();
  let parsed = val;
  if (type === 'integer') parsed = val === '' ? '' : parseInt(val, 10);
  else if (type === 'float') parsed = val === '' ? '' : parseFloat(val);
  setData(draft => { draft[name] = parsed; });
};


// Handles process after url has been redirected back from Github:
useEffect(() => {
  const params = new URLSearchParams(window.location.search);
  const code = params.get('code');
  if (localStorage.githubToken) {
    setLoggedIn(true);
    setLoggedInMessage(`Logged in as ${localStorage.username}`);
    return;
  }
  if (code) {
    axios.post(`${GITHUB_GATEWAY_PATH}verifyCode`, { code })
      .then(res => {
        if (res.data.authenticated) {
          const { token, username, admin_status } = res.data;
          localStorage.githubToken = token;
          localStorage.username   = username;
          localStorage.setItem('adminStatus', admin_status.toString());
          setLoggedIn(true);
          setLoggedInMessage(`Logged in as ${username}`);
          setAdminStatus(admin_status);     
          window.history.replaceState(null, '', window.location.pathname);
        }
      })
      .catch(() => setMessage('GitHub login failed'));
  }
}, []);
// Starts login process:
  const githubLogin = () => {
    window.location.assign(`https://github.com/login/oauth/authorize/?client_id=${OAUTH_ClientID}`);
  };
// Handles logout process:
  const handleLogout = () => {
    localStorage.clear();
    setLoggedIn(false);
    setAdminStatus(false)
    setLoggedInMessage(null);
    setMessage('Fill in the form');
    setUserName('');
    setBranch('main');
    setPullRequestUrl(null);
    setUploadStatus(null);
    resetData();
    setIsSubmitting(false);
    setLastSavedFingerprint('');
  }
    
// Handles form submission:
const handleSubmit = async e => {
  e.preventDefault();
  if (isSubmitting) return;

  const hasLipids   = Object.keys(data.LIPID_COMPOSITION).length > 0;
  const hasSolution = Object.keys(data.SOLUTION_COMPOSITION).length > 0;

  if (!hasLipids || !hasSolution) {
    setUploadStatus("Add at least one membrane and one solution molecule."); 
    return;
  }

  let infoFile;
  try {
    infoFile = CreateInfoFile(data);
  } catch (err) {
    console.error('CreateInfoFile error:', err);
    setUploadStatus(err.message || "Failed to create INFO file.");
    return; 
  }
  
  // Creates JSON payload from data
  const jsonPayload = {
    ...infoFile,
    userName,
    branch
  };
  const fingerprint = JSON.stringify(jsonPayload);
  if (fingerprint === lastSavedFingerprint) {
    setUploadStatus("No changes since last successful submit.");
    return;
  }


  setUploadStatus('Uploading data…');
  setIsSubmitting(true);


  try {
    const resp = await axios.post('/app/upload', jsonPayload, {
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${localStorage.githubToken}`
      }
    });
    setUploadStatus('Upload succeeded!');
    setLastSavedFingerprint(fingerprint);
    
    if (resp.data.pullUrl) {
      setPullRequestUrl(resp.data.pullUrl);
    }

  } catch (err) {
    const errorMsg = err.response?.data?.error ?? err.message;
    console.error('Upload error', err.response?.status, err.response?.data || err);
    setUploadStatus(`Upload failed: ${errorMsg}`);
  }
  finally {
    setIsSubmitting(false);
  }
};

return (
<div className="Container">
  <div className="Left">
    {loggedIn && (
      <>
        <button
          onClick={() => setShowInformation(prev => !prev)}
          className="button secondary"
        >
          {showInformation ? "Hide" : "Show"} Information
        </button>

        {showInformation && <Information />}
      </>
    )}
  </div>

    <div className="App">
      <header className="App-header">
        <h1>Welcome to the NMRLipids Upload Portal</h1>
      </header>
          {!loggedIn && (
       <> 
        <button
            onClick={githubLogin}
            className="button centered"
          >
            GitHub Login
          </button>
         <h3>Information</h3>
         <div className="info-block">
         <p>Please log in with GitHub to upload data.</p>
         <p>GitHub login is currently used as a way to authenticate users and reduce spam.</p>
         <p>The login process authorizes the app to access your public information, specifically your username. Nothing else.</p>
        </div>
        </>
      )}

      {loggedIn && (
        <>
          <input
            type="text"
            placeholder="Enter your name"
            value={userName}
            onChange={e => setUserName(e.target.value)}
            className="name-input centered"
          />
            <BranchSelect
              selectedBranch={branch}
              setSelectedBranch={setBranch}
              setMessage={setMessage}
              DataRepo={DataRepo}
            />
          {message && <p className="status-message-centered">Status: {message}</p>}

          <form onSubmit={handleSubmit} className="upload-form">
              <ScalarFields
                data={data}
                onChange={handleChange}
                fieldConfig={fieldConfig}
                dropdownOptions={dropdownOptions}
                />
              <UnitedAtomDictEditor data={data} setData={setData} />
              <CompositionInfo/>
              <CompositionEditor
                options={lipidList}
                mappingDict={mappingDict}  
                composition={data.LIPID_COMPOSITION}
                setComposition={recipe => setData(draft => { recipe(draft.LIPID_COMPOSITION); })}
                name="Membrane Composition"
              />
                <CompositionEditor
                options={solutionList}
                mappingDict={mappingDict}  
                composition={data.SOLUTION_COMPOSITION}
                setComposition={recipe => setData(draft => { recipe(draft.SOLUTION_COMPOSITION); })
                }
                name="Solution Composition"
              />
              <div className="submit-row">
                <button type="submit" className="button" disabled={isSubmitting}>
                  {isSubmitting ? "Submitting…" : "Submit"}
                </button>
               <p className="upload-status">
                {uploadStatus}
                </p>
                {pullRequestUrl && (
                  <a
                    href={pullRequestUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="button"
                  >View Pull Request</a>
                )}
              </div>
          </form>
                {/* Simple text link to docs */}
         <p className="docs-link">
          View full documentation on{' '}
          <a
            href="https://nmrlipids.github.io/FAIRMD_lipids/stable/schemas/simulation_metadata.html"
            target="_blank"
            rel="noopener noreferrer"
          >
            GitHub Pages
          </a>.
        </p>
        </>
      )}
    </div>

<div className="Right">
  {loggedIn && (
    <>
      <div className="Right-top">
        <button onClick={handleLogout} className="button secondary">
          Logout
        </button>
      </div>

      {adminStatus && (
        <div className="Right-center">
          <div className="Admin-panel">
            <h3>Administration panel</h3>
            <div className="refresh-panel">
              <button onClick={updateDatabankFiles} className="button secondary">
                Update databank files
              </button>
              {refreshMessage && <p className="centered">{refreshMessage}</p>}
            </div>
          </div>
        </div>
      )}
    </>
  )}
</div>
</div>
);
}
